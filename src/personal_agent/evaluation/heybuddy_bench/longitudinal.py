"""Explicit paid extension, frozen across dev/holdout and charged to the existing CNY20 cap."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from personal_agent.evaluation.heybuddy_bench.experiment import (
    ExperimentProvider,
    SessionBudget,
    cny,
    production_code_hash,
    write_json,
)
from personal_agent.evaluation.heybuddy_bench.long_data import DATA, score, validate
from personal_agent.evaluation.heybuddy_bench.long_replay import run_cases
from personal_agent.evaluation.heybuddy_bench.sequence_data import TRACKS, rows, sha
from personal_agent.evaluation.heybuddy_bench.sequence_data import score as score_short
from personal_agent.evaluation.runner import Budget
from personal_agent.file_lock import FileLock
from personal_agent.memory.privacy import require_private_directory


def implementation_hash():
    return hashlib.sha256(
        b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py")))
    ).hexdigest()


def select(root, stage):
    if stage == "short-reserved":
        return [
            c for c in rows(root.parent / "sequence_v1/inputs.jsonl") if c["split"] == "holdout"
        ]
    cases = rows(root / "long/inputs.jsonl")
    chosen = [c for c in cases if c["split"] == ("holdout" if stage == "reserved" else "dev")]
    if stage == "smoke":
        return [next(c for c in chosen if c["track"] == t) for t in TRACKS]
    # Balanced progress across tracks; never select by Gold or by observed success.
    groups = [[c for c in chosen if c["track"] == t] for t in TRACKS]
    return [c for group in zip(*groups, strict=True) for c in group]


def carried_budget(root, protocol, protocol_hash):
    path = root / "long-session.json"
    source = json.loads((root / "session.json").read_text(encoding="utf-8"))
    if source != protocol["carryover"] or source["pending"] or not source["usage_complete"]:
        raise ValueError("Prior ledger differs from frozen carryover; never reset spent allowance")
    budget = SessionBudget(path, protocol, protocol_hash)
    if not path.exists():
        for name in ("requests", "input_tokens", "output_tokens", "reasoning_tokens"):
            setattr(budget, name, source[name])
        budget.reserved_tokens = source["input_tokens"] + source["output_tokens"]
        budget.reserved_cost = source["cost_cny_upper_estimate"]
        write_json(path, budget.state())
    return budget


async def execute(args, stage, budget, provider=None):
    cases = select(args.dataset, stage)
    output = args.root / (("live-long-" if args.live else "contract-long-") + stage)
    if output.exists():
        raise ValueError("Stage already exists; preserve outcomes and billing")
    output.mkdir()
    with (
        (output / "predictions.jsonl").open("w", encoding="utf-8", newline="\n") as predictions,
        (output / "events.jsonl").open("w", encoding="utf-8", newline="\n") as events,
    ):

        def on_step(cid, sid, error):
            event = dict(case_id=cid, step=sid, error_code=error, requests=budget.requests)
            events.write(json.dumps(event) + "\n")
            events.flush()
            if sid == "review" or (sid.startswith("u") and int(sid[1:]) % 8 == 0):
                print(json.dumps(event), flush=True)

        def on_result(result):
            predictions.write(json.dumps(cny(result), ensure_ascii=False) + "\n")
            predictions.flush()
            print(
                json.dumps(
                    dict(
                        stage=stage,
                        case_id=result["case_id"],
                        steps=len(result["steps"]),
                        error_code=result["error_code"],
                        requests=budget.requests,
                        cost_cny_upper_estimate=budget.state()["cost_cny_upper_estimate"]
                        if args.live
                        else 0,
                    )
                ),
                flush=True,
            )

        measured = ExperimentProvider(provider, output) if provider is not None else None
        results = (
            []
            if budget.exhausted or (args.live and not budget.usage_complete)
            else await run_cases(
                cases,
                temp_parent=output / "temporary",
                budget=budget,
                provider=measured,
                short=stage == "short-reserved",
                on_result=on_result,
                on_step=on_step,
            )
        )
    if not args.live:
        report = dict(
            expected=len(cases),
            observed=len(results),
            capability_evaluation=False,
            note="Gold-independent offline wiring only; no capability accuracy",
        )
    elif stage == "short-reserved":
        report = score_short(cases, rows(args.dataset.parent / "sequence_v1/gold.jsonl"), results)
    else:
        report = score(cases, rows(args.dataset / "long/gold.jsonl"), results)
    report.update(
        stage=stage,
        usage=budget.state() if args.live else dict(api_requests=0, cost_cny_upper_estimate=0),
        errors=[
            r["case_id"]
            for r in results
            if r["error_code"] or any(s.get("error_code") for s in r["steps"])
        ],
        hashes=dict(
            suite=sha(args.dataset / "manifest.json"),
            evaluation=implementation_hash(),
            production=production_code_hash(),
        ),
        configuration=dict(
            memory="checkpoints consume NEW pending Runs; no automatic learning scheduler",
            proactive=(
                "real Scout per user Run; one final Attention checkpoint; "
                "compose/social windows disabled"
            ),
            routing="production rules and recent24/LLM12; actual prior bindings persist",
            assistant="fixed acknowledgement/context data; no generated end-to-end answer",
        ),
    )
    write_json(output / "report.json", report)
    print(
        json.dumps(
            dict(
                stage=stage,
                expected=len(cases),
                observed=len(results),
                errors=len(report["errors"]),
                usage=report["usage"],
            )
        ),
        flush=True,
    )
    return len(results) == len(cases) and not report["errors"]


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATA)
    parser.add_argument(
        "--root", type=Path, default=Path(".heybuddy/evaluations/sequence_20261110")
    )
    parser.add_argument(
        "--stage", choices=["smoke", "dev", "reserved", "short-reserved", "all"], default="smoke"
    )
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    metadata = validate(args.dataset)
    if args.validate:
        print(json.dumps(metadata, indent=2))
        return
    require_private_directory(args.root.absolute())
    args.root.mkdir(parents=True, exist_ok=True)
    protocol_path = args.dataset / "live_protocol.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    if (
        protocol["max_cost_cny"] != 20
        or protocol["input_price_cny"] != 2
        or protocol["output_price_cny"] != 8
        or protocol["dataset_sha256"] != sha(args.dataset / "manifest.json")
    ):
        raise ValueError("Protocol differs from frozen suite or authorized CNY20 cap")
    stages = protocol["stage_order"] if args.stage == "all" else [args.stage]
    with FileLock(args.root / "session.lock"):
        if not args.live:
            for stage in stages:
                await execute(args, stage, Budget(requests=4500, tokens=9000000))
            return
        from openai import AsyncOpenAI
        from pydantic import ValidationError

        from personal_agent.config import load_settings
        from personal_agent.llm.openai_provider import OpenAIProvider

        try:
            settings = load_settings()
        except (ValidationError, OSError):
            raise ValueError("Model configuration unavailable") from None
        endpoint = urlsplit(str(settings.base_url))
        if (
            settings.model != protocol["model"]
            or endpoint.hostname != protocol["endpoint_host"]
            or endpoint.scheme != "https"
            or endpoint.port not in {None, 443}
            or endpoint.username
            or endpoint.password
        ):
            raise ValueError("Configured model/HTTPS endpoint differs from priced protocol")
        freeze_path = args.root / "long-freeze.json"
        freeze = dict(
            protocol=sha(protocol_path),
            evaluation=implementation_hash(),
            production=production_code_hash(),
        )
        if freeze_path.exists() and json.loads(freeze_path.read_text(encoding="utf-8")) != freeze:
            raise ValueError("Implementation changed after freeze; do not tune on holdout")
        write_json(freeze_path, freeze)
        budget = carried_budget(args.root, protocol, sha(protocol_path))
        ok = True
        async with AsyncOpenAI(
            api_key=settings.api_key.get_secret_value(),
            base_url=str(settings.base_url),
            max_retries=0,
            timeout=60,
        ) as client:
            provider = OpenAIProvider(client, settings.model, json_mode=True)
            for stage in stages:
                ok = await execute(args, stage, budget, provider) and ok
        if not ok:
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
