"""Explicit, isolated sequence benchmark. --live alone permits paid model calls."""

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
from personal_agent.evaluation.heybuddy_bench.sequence_data import (
    DATA,
    TRACKS,
    rows,
    score,
    sha,
    validate,
)
from personal_agent.evaluation.heybuddy_bench.sequence_replay import run_sequences
from personal_agent.evaluation.runner import Budget
from personal_agent.file_lock import FileLock
from personal_agent.memory.privacy import require_private_directory


def select(cases, stage):
    dev = [c for c in cases if c["split"] == "dev"]
    if stage == "smoke":
        return [next(c for c in dev if c["track"] == track) for track in TRACKS]
    return dev


def code_hash():
    return hashlib.sha256(
        b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("sequence*.py")))
    ).hexdigest()


async def execute(args, cases, root, budget, provider=None):
    output = root / (("live-" if args.live else "contract-") + args.stage)
    if output.exists():
        raise ValueError("Stage exists; preserve previous predictions and the cumulative ledger")
    output.mkdir()
    with (output / "predictions.jsonl").open("w", encoding="utf-8", newline="\n") as stream:

        def save(result):
            stream.write(json.dumps(cny(result), ensure_ascii=False) + "\n")
            stream.flush()
            print(
                json.dumps(
                    dict(
                        case_id=result["case_id"],
                        checkpoints=len(result["steps"]),
                        error=result["error_code"],
                        requests=budget.requests,
                    ),
                    ensure_ascii=False,
                ),
                flush=True,
            )

        if provider is not None:
            provider = ExperimentProvider(provider, output)
        predictions = await run_sequences(
            cases,
            temp_parent=output / "temporary",
            budget=budget,
            provider=provider,
            on_result=save,
        )
    if args.live:
        # The replay has no gold argument; labels enter only this post-inference scorer.
        report = score(cases, rows(args.dataset / "gold.jsonl"), predictions)
    else:
        report = dict(
            expected=len(cases),
            observed=len(predictions),
            capability_evaluation=False,
            note="Offline contract responses exercise wiring only; no capability accuracy.",
        )
    report.update(
        stage=args.stage,
        live=args.live,
        usage=cny(budget.state()) if args.live else dict(api_requests=0, api_cost_cny=0),
        errors=[
            r["case_id"]
            for r in predictions
            if r["error_code"] or any(s.get("error_code") for s in r["steps"])
        ],
        hashes=dict(
            evaluation=code_hash(),
            production=production_code_hash(),
            **{n: sha(args.dataset / n) for n in ["inputs.jsonl", "gold.jsonl"]},
        ),
        configuration=dict(
            memory="explicit per-step learning checkpoints; persistent MemoryFS",
            proactive=(
                "production defaults except compose=false and social_followup=false; "
                "one Attention attempt per requested checkpoint"
            ),
            routing="actual recent24/LLM12 and rules; persistent bindings",
        ),
    )
    write_json(output / "report.json", report)
    print(
        json.dumps(
            dict(
                report=str(output / "report.json"),
                expected=len(cases),
                observed=len(predictions),
                usage=report["usage"],
            ),
            ensure_ascii=False,
        ),
        flush=True,
    )
    if len(predictions) != len(cases) or report["errors"]:
        raise SystemExit(1)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATA)
    parser.add_argument(
        "--root", type=Path, default=Path(".heybuddy/evaluations/sequence_20261110")
    )
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--stage", choices=["smoke", "dev"], default="smoke")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    metadata = validate(args.dataset)
    if args.validate:
        print(json.dumps(metadata, ensure_ascii=False, indent=2))
        return
    cases = select(rows(args.dataset / "inputs.jsonl"), args.stage)
    require_private_directory(args.root.absolute())
    args.root.mkdir(parents=True, exist_ok=True)
    if not args.live:
        with FileLock(args.root / "session.lock"):
            await execute(args, cases, args.root, Budget(requests=700, tokens=8000000))
        return
    protocol_path = args.dataset / "live_protocol.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    if not 0 < protocol["max_cost_cny"] <= 20 or protocol["live_split"] != "dev":
        raise ValueError("User authorization is capped at CNY20, development only")
    if (
        protocol["dataset_hashes"]
        != json.loads((args.dataset / "manifest.json").read_text(encoding="utf-8"))["sha256"]
    ):
        raise ValueError("Protocol and dataset differ")
    from openai import AsyncOpenAI
    from pydantic import ValidationError

    from personal_agent.config import load_settings
    from personal_agent.llm.openai_provider import OpenAIProvider

    try:
        settings = load_settings()  # Trusted .env loader. Never display Settings or secret values.
    except (ValidationError, OSError):
        raise ValueError("Evaluation configuration unavailable") from None
    endpoint = urlsplit(str(settings.base_url))
    if (
        settings.model != protocol["model"]
        or endpoint.hostname != protocol["endpoint_host"]
        or endpoint.scheme != "https"
        or endpoint.port not in {None, 443}
        or endpoint.username
        or endpoint.password
    ):
        raise ValueError("Model or endpoint differs from priced protocol")
    with FileLock(args.root / "session.lock"):
        freeze_path = args.root / "freeze.json"
        freeze = dict(
            protocol=sha(protocol_path), evaluation=code_hash(), production=production_code_hash()
        )
        if freeze_path.exists() and json.loads(freeze_path.read_text(encoding="utf-8")) != freeze:
            raise ValueError("Frozen implementation changed; do not tune on live outcomes")
        write_json(freeze_path, freeze)
        budget = SessionBudget(args.root / "session.json", protocol, sha(protocol_path))
        async with AsyncOpenAI(
            api_key=settings.api_key.get_secret_value(),
            base_url=str(settings.base_url),
            max_retries=0,
            timeout=60,
        ) as client:
            await execute(
                args,
                cases,
                args.root,
                budget,
                OpenAIProvider(client, settings.model, json_mode=True),
            )


if __name__ == "__main__":
    asyncio.run(main())
