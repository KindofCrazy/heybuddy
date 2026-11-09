"""Explicit live improvement rounds sharing a carried, persistent CNY budget."""

import argparse
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from personal_agent.evaluation.heybuddy_bench.__main__ import select
from personal_agent.evaluation.heybuddy_bench.dataset import (
    load_inputs,
    read_rows,
    sha256,
    validate,
)
from personal_agent.evaluation.heybuddy_bench.experiment import (
    ExperimentProvider,
    SessionBudget,
    cny,
    production_code_hash,
    write_json,
)
from personal_agent.evaluation.heybuddy_bench.fact_scoring import score_facts
from personal_agent.evaluation.heybuddy_bench.runner import run_cases
from personal_agent.evaluation.heybuddy_bench.scoring import aggregate
from personal_agent.evaluation.heybuddy_bench.state_scoring import score_state
from personal_agent.evaluation.heybuddy_bench.temporal_scoring import score_temporal
from personal_agent.evaluation.runner import Budget
from personal_agent.file_lock import FileLock
from personal_agent.memory.privacy import require_private_directory

DATA = Path(__file__).resolve().parents[4] / "evals/heybuddy_bench"


def carry_budget(root: Path, protocol: dict, checksum: str) -> SessionBudget:
    path = root / "session.json"
    if not path.exists():
        previous = Path(protocol["carry_ledger"])
        if not previous.exists():
            previous = DATA / "v0_4/budget_start.json"
        if sha256(previous) != protocol["carry_ledger_sha256"]:
            raise ValueError("Previous ledger changed; review before carrying budget")
        state = json.loads(previous.read_text(encoding="utf-8"))
        if state["pending"] or not state["usage_complete"]:
            raise ValueError("Previous usage is uncertain; no new requests allowed")
        budget = SessionBudget(path, protocol, checksum)
        for key in ("requests", "input_tokens", "output_tokens", "reasoning_tokens"):
            setattr(budget, key, state[key])
        actual = (
            budget.input_tokens * budget.input_price + budget.output_tokens * budget.output_price
        ) / 1e6
        if abs(actual - state["cost_cny_upper_estimate"]) > 0.0000001:
            raise ValueError("Carry prices differ; do not silently reset prior cost")
        budget.reserved_tokens = budget.input_tokens + budget.output_tokens
        budget.reserved_cost = actual
        write_json(path, budget.state())
    return SessionBudget(path, protocol, checksum)


def selected_cases(part: str, revision: str = "v0_3") -> list[dict]:
    if part == "diagnostic":
        manifest = json.loads((DATA / "review_probes/manifest.json").read_text())
        for name, checksum in manifest["sha256"].items():
            if sha256(DATA / "review_probes" / name) != checksum:
                raise ValueError("Diagnostic data changed")
        return load_inputs(DATA / "review_probes")
    primary = load_inputs(DATA / revision)
    challenge = load_inputs(DATA / challenge_revision(revision))

    def reserved(case):
        return int(case["case_id"].rsplit("_", 1)[1]) > 5

    if part == "reserved":
        return [case for case in challenge if reserved(case)]
    if part == "smoke":
        return select(primary, 3)
    return primary + [case for case in challenge if not reserved(case)]


def challenge_revision(revision: str) -> str:
    return revision + "_challenge" if revision in {"v0_3_2", "v0_3_3", "v0_4"} else "v0_3_challenge"


def summarize(cases: list[dict], rows: list[dict], revision: str = "v0_3") -> dict:
    # Called only after the task provider is closed. No label reaches run_cases.
    all_gold = read_rows(DATA / revision / "gold.jsonl") + read_rows(
        DATA / challenge_revision(revision) / "gold.jsonl"
    )
    gold = {row["case_id"]: row for row in all_gold}
    if any(c["case_id"].startswith("probe_") for c in cases):
        gold.update({r["case_id"]: r for r in read_rows(DATA / "review_probes/gold.jsonl")})
    for case in cases:
        if case["track"] == "memory_learning":
            gold[case["case_id"]]["gold"]["_memory_before"] = case["input"]["memory_files"]
    reports = {}
    for group, chosen in (
        ("primary", [c for c in cases if not c["case_id"].startswith(("challenge_", "probe_"))]),
        ("challenge", [c for c in cases if c["case_id"].startswith("challenge_")]),
        ("diagnostic", [c for c in cases if c["case_id"].startswith("probe_")]),
    ):
        if not chosen:
            continue
        ids = {c["case_id"] for c in chosen}
        report = cny(
            aggregate(
                chosen,
                [gold[c["case_id"]] for c in chosen],
                [r for r in rows if r["case_id"] in ids],
                score_fn=(
                    score_temporal
                    if revision in {"v0_3_3", "v0_4"}
                    else score_facts
                    if revision == "v0_3_2"
                    else score_state
                ),
            )
        )
        by_id = {r["case_id"]: r for r in rows}
        for detail in report["cases"]:
            row = by_id.get(detail["case_id"], {})
            prediction = row.get("prediction", {})
            detail.update(
                error_code=prediction.get("error_code")
                or prediction.get("runtime", {}).get("error_code")
                or row.get("error_code"),
                usage=cny(row.get("usage", {})),
                routing_reason=prediction.get("routing_reason"),
                targets=[c["path"] for c in prediction.get("changes", [])]
                or prediction.get("proposal_ids", [])
                or [prediction.get("thread_id")],
                trace_stages=[
                    dict(schema=c["schema"], status=c["status"], prompt_sha256=c["prompt_sha256"])
                    for c in row.get("calls", [])
                ],
            )
        reports[group] = report
    return reports


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True)
    parser.add_argument(
        "--part", choices=["smoke", "development", "reserved", "diagnostic"], required=True
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--track", choices=["thread_routing", "memory_learning", "proactive_decision"]
    )
    parser.add_argument(
        "--revision", choices=["v0_3", "v0_3_1", "v0_3_2", "v0_3_3", "v0_4"], default="v0_3"
    )
    parser.add_argument(
        "--root", type=Path, default=Path(".heybuddy/evaluations/hb-cycle-20261108-v3")
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", args.phase):
        parser.error("Invalid phase name")
    if args.phase != "baseline" and not args.live:
        parser.error("Real evaluation requires explicit --live")
    if args.phase == "baseline" and args.live:
        parser.error("Baseline does not use a model")
    protocol_path = DATA / "v0_3/cycle_protocol.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    if sha256(Path(__file__).with_name("state_scoring.py")) != protocol["scoring_sha256"]:
        raise ValueError("Frozen state scorer changed")
    for name, checksum in protocol["data_hashes"].items():
        if sha256(DATA / name) != checksum:
            raise ValueError("Frozen evaluation file changed")
    validate(DATA / args.revision)
    validate(DATA / challenge_revision(args.revision), expected=10)
    if args.revision in {"v0_3_2", "v0_3_3", "v0_4"}:
        manifest = json.loads((DATA / args.revision / "manifest.json").read_text())
        if manifest["fact_scorer_sha256"] != sha256(Path(__file__).with_name("fact_scoring.py")):
            raise ValueError("Frozen cross-file scorer changed")
        if args.revision in {"v0_3_3", "v0_4"} and manifest["temporal_scorer_sha256"] != sha256(
            Path(__file__).with_name("temporal_scoring.py")
        ):
            raise ValueError("Frozen temporal scorer changed")
    cases = selected_cases(args.part, args.revision)
    if args.track:
        cases = [case for case in cases if case["track"] == args.track]
    require_private_directory(args.root.absolute())
    args.root.mkdir(parents=True, exist_ok=True)
    output = args.root / (args.phase + "-" + args.part)
    if output.exists():
        raise ValueError("Preserve prior outputs; choose a new phase")
    with FileLock(args.root / "session.lock"):
        folders = [args.revision, challenge_revision(args.revision)]
        if args.part == "diagnostic":
            folders.append("review_probes")
        data_snapshot = {
            folder + "/" + name: sha256(DATA / folder / name)
            for folder in folders
            for name in ["inputs.jsonl", "gold.jsonl", "manifest.json"]
        }
        data_freeze = args.root / (args.phase + "-data-freeze.json")
        if data_freeze.exists() and json.loads(data_freeze.read_text()) != data_snapshot:
            raise ValueError("Dataset changed within a frozen phase")
        if not data_freeze.exists():
            write_json(data_freeze, data_snapshot)
        frozen = args.root / (args.phase + "-freeze.json")
        code_hash = production_code_hash()
        if frozen.exists():
            if json.loads(frozen.read_text())["production_code_hash"] != code_hash:
                raise ValueError("Code changed within a frozen phase")
        else:
            write_json(
                frozen, dict(production_code_hash=code_hash, protocol_hash=sha256(protocol_path))
            )
        output.mkdir()
        budget = carry_budget(args.root, protocol, sha256(protocol_path)) if args.live else Budget()
        write_json(
            output / "provenance.json",
            dict(
                production_code_hash=code_hash,
                protocol_hash=sha256(protocol_path),
                part=args.part,
                phase=args.phase,
                track_filter=args.track,
                revision=args.revision,
                data_snapshot=data_snapshot,
                model=protocol["model"] if args.live else None,
                endpoint_host=protocol["endpoint_host"] if args.live else None,
            ),
        )

        async def run(provider=None):
            with (output / "predictions.jsonl").open("w", encoding="utf-8", newline="\n") as stream:

                def save(row):
                    stream.write(json.dumps(cny(row), ensure_ascii=False) + "\n")
                    stream.flush()

                return await run_cases(
                    cases,
                    mode="live" if args.live else "baseline",
                    provider=provider,
                    budget=budget,
                    temp_parent=output / "temporary",
                    on_result=save,
                )

        if args.live:
            from openai import AsyncOpenAI
            from pydantic import ValidationError

            from personal_agent.config import load_settings
            from personal_agent.llm.openai_provider import OpenAIProvider

            try:
                settings = load_settings()  # Authorized loader; never dump settings.
            except (ValidationError, OSError):
                raise ValueError("Evaluation configuration is unavailable") from None
            endpoint = urlsplit(str(settings.base_url))
            if (
                endpoint.scheme != "https"
                or endpoint.hostname != protocol["endpoint_host"]
                or endpoint.port not in {None, 443}
                or settings.model != protocol["model"]
            ):
                raise ValueError("Endpoint/model differs from approved protocol")
            async with AsyncOpenAI(
                api_key=settings.api_key.get_secret_value(),
                base_url=str(settings.base_url),
                max_retries=0,
                timeout=60,
            ) as client:
                rows = await run(
                    ExperimentProvider(
                        OpenAIProvider(client, settings.model, json_mode=True), output
                    )
                )
        else:
            rows = await run()
        report = dict(
            phase=args.phase,
            part=args.part,
            observed=len(rows),
            expected=len(cases),
            reports=summarize(cases, rows, args.revision) if args.part != "smoke" else {},
            ledger=budget.state() if args.live else dict(cost_cny_upper_estimate=0),
        )
        write_json(output / "report.json", report)
        print(
            json.dumps(
                dict(
                    phase=args.phase,
                    part=args.part,
                    observed=len(rows),
                    tracks={
                        g: {t: v["decision_accuracy"] for t, v in r["tracks"].items()}
                        for g, r in report["reports"].items()
                    },
                    ledger=report["ledger"],
                )
            )
        )
        if len(rows) != len(cases) or any(r["prediction"]["action"] == "__error__" for r in rows):
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
