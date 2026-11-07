"""python -m personal_agent.evaluation.heybuddy_bench --help"""

import argparse
import asyncio
import hashlib
import json
import os
from collections import Counter
from pathlib import Path

from personal_agent.evaluation.heybuddy_bench.dataset import (
    TRACKS,
    load_inputs,
    read_rows,
    sha256,
    validate,
)
from personal_agent.evaluation.heybuddy_bench.runner import run_cases
from personal_agent.evaluation.heybuddy_bench.scoring import aggregate
from personal_agent.evaluation.runner import Budget


def select(cases, limit):
    """Deterministic category round-robin, independent of gold and method."""
    selected = []
    for track in TRACKS:
        rows = [c for c in cases if c["track"] == track]
        categories = {}
        for row in rows:
            categories.setdefault(row["category"], []).append(row)
        ordered = []
        while any(categories.values()):
            for values in categories.values():
                if values:
                    ordered.append(values.pop(0))
        selected.extend(ordered[:limit] if limit else ordered)
    return selected


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("evals/heybuddy_bench"))
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--mode", choices=["baseline", "smoke", "live"], default="smoke")
    parser.add_argument("--limit-per-track", type=int, default=3, help="0 = all 50 per track")
    parser.add_argument("--output", type=Path, default=Path(".cache/heybuddy-bench-run"))
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--max-requests", type=int, default=30)
    parser.add_argument("--max-tokens", type=int, default=1000000)
    parser.add_argument("--input-price", type=float, help="USD per million tokens")
    parser.add_argument("--output-price", type=float, help="USD per million tokens")
    parser.add_argument("--max-cost", type=float, default=1.0, help="USD reservation cap for live")
    args = parser.parse_args()
    if args.validate:
        print(json.dumps(validate(args.dataset), ensure_ascii=False, indent=2))
        return
    if args.limit_per_track < 0:
        parser.error("limit must be nonnegative")
    cases = select(load_inputs(args.dataset), args.limit_per_track)
    if args.output.exists():
        parser.error("Output already exists; choose a new directory")
    provider, client = None, None
    if args.mode == "live":
        # Explicit env only: never load the user's .env/application Settings/real data paths.
        key = os.environ.get("OPENAI_API_KEY") or os.environ.get("AGENT_API_KEY")
        if not key or not args.model or args.input_price is None or args.output_price is None:
            parser.error("Live needs API key env, --model and explicit input/output USD prices")
        from openai import AsyncOpenAI

        from personal_agent.llm.openai_provider import OpenAIProvider

        client = AsyncOpenAI(api_key=key, base_url=args.base_url, max_retries=0, timeout=60)
        provider = OpenAIProvider(client, args.model, json_mode=True)
    budget = Budget(
        args.max_requests,
        args.max_tokens,
        input_price=args.input_price,
        output_price=args.output_price,
        max_cost=args.max_cost if args.mode == "live" else None,
    )
    args.output.mkdir(parents=True)
    try:
        with (args.output / "predictions.jsonl").open(
            "w", encoding="utf-8", newline="\n"
        ) as stream:

            def save(result):
                stream.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
                stream.flush()

            results = await run_cases(
                cases,
                mode=args.mode,
                temp_parent=args.output / "temporary",
                provider=provider,
                budget=budget,
                on_result=save,
            )
    finally:
        if client:
            await client.close()
    # Only after every prediction is finalized do we open the separate gold file.
    if args.mode == "smoke":
        report = dict(
            expected=len(cases),
            observed=len(results),
            counts=dict(Counter(c["track"] for c in cases)),
            errors=[r["case_id"] for r in results if r["prediction"]["action"] == "__error__"],
            provider_calls=sum(r["usage"]["provider_calls"] for r in results),
            api_requests=0,
            api_cost_usd=0,
            note="Contract test with gold-independent responses; no capability scores.",
        )
    else:
        gold = [
            g
            for g in read_rows(args.dataset / "gold.jsonl")
            if g["case_id"] in {c["case_id"] for c in cases}
        ]
        report = aggregate(cases, gold, results)
    report.update(
        mode=args.mode,
        model=args.model,
        backbone="configured Provider" if provider else None,
        capability_evaluation=args.mode != "smoke",
        human_reviewed=False,
        dataset_hashes={
            name: sha256(args.dataset / name) for name in ("inputs.jsonl", "gold.jsonl")
        },
        configuration=dict(
            router_model_selection=True,
            router_defer_weak_rules=False,
            context_window=131072,
            memory_policy="production defaults",
            proactive_policy="production defaults; compose disabled",
            max_requests=args.max_requests,
            max_tokens=args.max_tokens,
            max_cost_usd=args.max_cost if args.mode == "live" else 0,
            input_price_usd=args.input_price,
            output_price_usd=args.output_price,
        ),
        evaluation_code_sha256=hashlib.sha256(
            b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py")))
        ).hexdigest(),
    )
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "mode": args.mode,
                "expected": len(cases),
                "observed": len(results),
                "report": str(args.output / "report.json"),
            },
            ensure_ascii=False,
        )
    )
    if len(results) != len(cases) or any(r["prediction"]["action"] == "__error__" for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
