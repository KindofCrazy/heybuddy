"""Explicit paid diagnostic experiment, using a frozen protocol and shared CNY ledger."""

import argparse
import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from personal_agent.evaluation.heybuddy_bench.__main__ import select
from personal_agent.evaluation.heybuddy_bench.dataset import (
    load_inputs,
    read_rows,
    sha256,
    validate,
)
from personal_agent.evaluation.heybuddy_bench.runner import run_cases
from personal_agent.evaluation.heybuddy_bench.scoring import aggregate, score_case
from personal_agent.evaluation.runner import Budget, EvaluationLimit
from personal_agent.file_lock import FileLock
from personal_agent.memory.privacy import require_private_directory


def write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def cny(value):
    """The shared Budget's arithmetic is currency-neutral; relabel its legacy USD keys."""
    if isinstance(value, list):
        return [cny(x) for x in value]
    if isinstance(value, dict):
        return {
            k.replace("api_cost_usd", "cost_cny_upper_estimate"): cny(v) for k, v in value.items()
        }
    return value


def production_code_hash() -> str:
    root = Path(__file__).resolve().parents[2]
    return hashlib.sha256(
        b"".join(
            path.read_bytes()
            for path in sorted(root.rglob("*.py"))
            if "evaluation" not in path.relative_to(root).parts
        )
    ).hexdigest()


class SessionBudget(Budget):
    """Reserve one serial request, then release unused capacity after known billed usage.

    A persistent pending/unknown request freezes the whole experiment, including future
    processes. Input reserves use a UTF-8 byte ceiling plus protocol overhead, not a guessed
    tokenizer. Reasoning is already included in output_tokens.
    """

    def __init__(self, path: Path, protocol: dict, protocol_hash: str):
        super().__init__(
            protocol["max_requests"],
            protocol["max_tokens"],
            input_price=protocol["input_price_cny"],
            output_price=protocol["output_price_cny"],
            max_cost=protocol["max_cost_cny"],
        )
        self.path, self.protocol_hash = path, protocol_hash
        self.pending = False
        if path.exists():
            state = json.loads(path.read_text(encoding="utf-8"))
            if (
                state["protocol_hash"] != protocol_hash
                or state["pending"]
                or not state["usage_complete"]
            ):
                raise EvaluationLimit("experiment_ledger_requires_review")
            for name in ("requests", "input_tokens", "output_tokens", "reasoning_tokens"):
                setattr(self, name, state[name])
            self.reserved_tokens = self.input_tokens + self.output_tokens
            self.reserved_cost = state["cost_cny_upper_estimate"]

    def state(self):
        return dict(
            protocol_hash=self.protocol_hash,
            currency="CNY",
            requests=self.requests,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            reasoning_tokens=self.reasoning_tokens,
            pending=self.pending,
            usage_complete=self.usage_complete,
            reserved_tokens=self.reserved_tokens,
            reserved_cost_cny=self.reserved_cost,
            cost_cny_upper_estimate=(
                self.input_tokens * self.input_price + self.output_tokens * self.output_price
            )
            / 1_000_000,
        )

    def reserve(self, messages, tools, output_limit):
        if self.pending or not self.usage_complete or tools:
            raise EvaluationLimit("experiment_ledger_requires_review")
        ceiling = sum(len(m.model_dump_json().encode()) for m in messages) + 4096
        tokens = ceiling + (output_limit or 0)
        cost = (ceiling * self.input_price + (output_limit or 0) * self.output_price) / 1_000_000
        if (
            self.requests >= self.max_requests
            or self.reserved_tokens + tokens > self.max_tokens
            or self.reserved_cost + cost > self.max_cost
        ):
            self.exhausted = True
            raise EvaluationLimit("experiment_budget_exhausted")
        self.requests += 1
        self.reserved_tokens += tokens
        self.reserved_cost += cost
        self.pending = True
        write_json(self.path, self.state())

    def observe(self, usage):
        super().observe(usage)
        if usage is not None:
            self.pending = False
            self.reserved_tokens = self.input_tokens + self.output_tokens
            self.reserved_cost = (
                self.input_tokens * self.input_price + self.output_tokens * self.output_price
            ) / 1_000_000
        write_json(self.path, self.state())


class ExperimentProvider:
    def __init__(self, provider, path: Path):
        self.provider, self.path = provider, path
        self.count = 0

    async def generate(self, messages, tools, **options):
        # These private artifacts contain only the synthetic/public evaluation fixture.
        # The task model receives no protocol labels, gold, case metadata or tools.
        self.count += 1
        write_json(
            self.path / f"request-{self.count:04d}.json",
            dict(
                messages=[m.model_dump(mode="json") for m in messages],
                options={**options, "reasoning_effort": "none"},
            ),
        )
        response = await self.provider.generate(
            messages, tools, **{**options, "reasoning_effort": "none"}
        )
        if not response.tool_calls and len(response.text.encode()) <= 32768:
            write_json(self.path / f"response-{self.count:04d}.json", dict(text=response.text))
        return response


def reviewed_score(cases, gold, results, protocol):
    """Predeclared alternatives are secondary; always preserve the unmodified v0.2 score."""
    by_id = {r["case_id"]: r["prediction"] for r in results}
    gold_by_id = {r["case_id"]: r["gold"] for r in gold}
    details = []
    for case in cases:
        cid = case["case_id"]
        original = gold_by_id[cid]
        prediction = copy.deepcopy(by_id.get(cid, {"action": "__missing__"}))
        if case["track"] == "memory_learning" and prediction.get("action") == "create":
            from personal_agent.core.memory import CoreMemoryError
            from personal_agent.memory.store import MemoryFS

            fs = MemoryFS(Path(".cache/benchmark-validation"))
            for change in prediction.get("changes", []):
                try:
                    fs.path(change["path"])
                    if "/" in change["path"] and not change["path"].endswith("/MEMORY.md"):
                        change["path"] = "topics/valid-new-topic.md"
                except CoreMemoryError:
                    pass
        if (
            case["track"] == "proactive_decision"
            and prediction.get("action") == "silent"
            and "silent" in original.get("acceptable_actions", [])
        ):
            prediction["proposal_ids"] = original["proposal_ids"]
        scores = [score_case(case["track"], original, prediction)]
        for alternative in protocol["review_alternatives"].get(cid, []):
            changed = {**original, **alternative, "acceptable_actions": [alternative["action"]]}
            scores.append(score_case(case["track"], changed, prediction))
        details.append(
            dict(
                case_id=cid,
                track=case["track"],
                reviewed_correct=any(s["decision_correct"] for s in scores),
                content_check_pass=scores[0]["content_check_pass"],
            )
        )
    return details


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage",
        required=True,
        choices=["smoke", "original-dev", "original-holdout", "candidate-dev", "candidate-holdout"],
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--candidate-tag", choices=["candidate", "final"], default="candidate")
    parser.add_argument("--dataset", type=Path, default=Path("evals/heybuddy_bench"))
    args = parser.parse_args()
    protocol_path = args.dataset / "live_protocol.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    validate(args.dataset)
    for name, checksum in protocol["dataset_hashes"].items():
        if sha256(args.dataset / name) != checksum:
            raise ValueError("Frozen dataset changed")
    cases = load_inputs(args.dataset)
    if args.stage == "smoke":
        cases = select([c for c in cases if c["case_id"] in protocol["development"]], 3)
    else:
        ids = protocol["holdout" if args.stage.endswith("holdout") else "development"]
        cases = [c for c in cases if c["case_id"] in ids]
    require_private_directory(args.root.absolute())
    stage_name = args.stage.replace("candidate", args.candidate_tag, 1)
    output = args.root / stage_name
    if output.exists():
        raise ValueError("Stage output exists; preserve prior results")
    args.root.mkdir(parents=True, exist_ok=True)
    from openai import AsyncOpenAI
    from pydantic import ValidationError

    from personal_agent.config import load_settings
    from personal_agent.llm.openai_provider import OpenAIProvider

    try:
        settings = load_settings()  # Trusted loader; never dump settings or secret content.
    except (ValidationError, OSError):
        raise ValueError("Evaluation configuration is unavailable") from None
    endpoint = urlsplit(str(settings.base_url))
    host = endpoint.hostname
    if (
        settings.model != protocol["model"]
        or host != protocol["endpoint_host"]
        or endpoint.scheme != "https"
        or endpoint.port not in {None, 443}
    ):
        raise ValueError("Configured model/endpoint differs from frozen protocol")
    with FileLock(args.root / "session.lock"):
        budget = SessionBudget(args.root / "session.json", protocol, sha256(protocol_path))
        output.mkdir()
        production_hash = production_code_hash()
        if args.stage.startswith("candidate"):
            frozen = json.loads(
                (args.root / (args.candidate_tag + "-freeze.json")).read_text(encoding="utf-8")
            )
            if frozen["production_code_hash"] != production_hash:
                raise ValueError("Candidate changed after freeze")
        write_json(
            output / "provenance.json",
            dict(
                protocol_hash=sha256(protocol_path),
                production_code_hash=production_hash,
                model=settings.model,
                endpoint_host=host,
                reasoning_effort="none",
                dataset_hashes=protocol["dataset_hashes"],
            ),
        )
        async with AsyncOpenAI(
            api_key=settings.api_key.get_secret_value(),
            base_url=str(settings.base_url),
            max_retries=0,
            timeout=60,
        ) as client:
            provider = ExperimentProvider(
                OpenAIProvider(client, settings.model, json_mode=True), output
            )
            with (output / "predictions.jsonl").open("w", encoding="utf-8", newline="\n") as stream:

                def save(result):
                    stream.write(json.dumps(cny(result), ensure_ascii=False) + "\n")
                    stream.flush()

                results = await run_cases(
                    cases,
                    mode="live",
                    temp_parent=output / "temporary",
                    provider=provider,
                    budget=budget,
                    on_result=save,
                )
        # Gold is used solely after inference. Reused holdout rounds are disclosed in the report.
        gold = [
            g
            for g in read_rows(args.dataset / "gold.jsonl")
            if g["case_id"] in {c["case_id"] for c in cases}
        ]
        report = cny(aggregate(cases, gold, results))
        report.update(
            stage=stage_name,
            protocol_hash=sha256(protocol_path),
            reviewed=reviewed_score(cases, gold, results, protocol),
            ledger=budget.state(),
        )
        write_json(output / "report.json", report)
        print(
            json.dumps(
                dict(
                    stage=stage_name,
                    observed=len(results),
                    expected=len(cases),
                    session=budget.state(),
                ),
                ensure_ascii=False,
            )
        )
        if len(results) != len(cases) or any(
            r["prediction"]["action"] == "__error__" for r in results
        ):
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
