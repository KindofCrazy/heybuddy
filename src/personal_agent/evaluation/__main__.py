"""Explicit live entry point, independent of pytest and normal CLI conversations."""

import argparse
import asyncio
import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

from openai import AsyncOpenAI
from pydantic import ValidationError

from personal_agent.config import load_settings
from personal_agent.evaluation.cases import CASES
from personal_agent.evaluation.prompts import capture, digest_snapshot
from personal_agent.evaluation.runner import Budget, evaluate_case
from personal_agent.llm.openai_provider import OpenAIProvider
from personal_agent.memory.privacy import require_private_directory


def corpus_hash() -> str:
    root = Path(__file__).parent
    return hashlib.sha256(
        (root / "cases.py").read_bytes() + (root / "scoring.py").read_bytes()
    ).hexdigest()


async def live(args, settings, prompts):
    output = args.output / ("run-" + args.variant + "-" + uuid4().hex[:8])
    output.mkdir(parents=True)
    require_private_directory(output)
    budget = Budget(
        args.max_requests,
        args.max_tokens,
        input_price=args.input_price,
        output_price=args.output_price,
        max_cost=args.max_cost,
    )
    selected = [
        case
        for case in CASES
        if (args.cases == "all" or case.id in args.cases.split(","))
        and (args.split == "all" or case.split == args.split)
    ]
    if not selected:
        raise ValueError("No cases selected")
    metadata = {
        "variant": args.variant,
        "model": settings.model,
        "endpoint": str(settings.base_url) if settings.base_url else "https://api.openai.com",
        "context_window": settings.context_window,
        "repetitions": args.repetitions,
        "case_ids": [case.id for case in selected],
        "corpus_hash": corpus_hash(),
        "prompt_hash": digest_snapshot(prompts),
        "parameters": {
            "normal_output": settings.reserved_output_tokens,
            "max_rounds": 5,
            "reflection": settings.reflection_policy().model_dump(),
        },
        "results": [],
    }
    # No API key/header/real source paths are serialized. Configured URL must not contain userinfo.
    from urllib.parse import urlsplit

    parsed = urlsplit(metadata["endpoint"])
    metadata["endpoint"] = parsed.hostname
    (output / "prompts.json").write_text(
        json.dumps(prompts, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    semaphore = asyncio.Semaphore(args.concurrency)
    async with AsyncOpenAI(
        api_key=settings.api_key.get_secret_value(),
        base_url=str(settings.base_url) if settings.base_url else None,
        max_retries=0,
        timeout=settings.request_timeout_seconds,
    ) as client:
        provider = OpenAIProvider(client, settings.model)

        async def one(case, repeat):
            async with semaphore:
                if budget.exhausted:
                    return
                folder = output / f"{case.id}-{repeat}"
                try:
                    result = await evaluate_case(
                        case,
                        folder,
                        provider,
                        prompts,
                        budget,
                        settings.context_window,
                        settings.reflection_policy(),
                        settings.reserved_output_tokens,
                    )
                    row = {
                        "case": case.id,
                        "repeat": repeat,
                        "split": case.split,
                        "pass": result["grading"]["pass"],
                        "score": result["grading"]["score"],
                        "checks": result["grading"]["checks"],
                        "seconds": result["seconds"],
                        "calls": len(result["calls"]),
                        "automation_requests": sum(
                            call["stage"] in {"worthiness", "extract", "select", "consolidate"}
                            for call in result["calls"]
                        ),
                    }
                except Exception as error:
                    row = {
                        "case": case.id,
                        "repeat": repeat,
                        "split": case.split,
                        "pass": False,
                        "error_type": type(error).__name__,
                    }
                metadata["results"].append(row)
                metadata["usage"] = budget.report()
                (output / "report.json").write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(json.dumps(row, ensure_ascii=False), flush=True)

        await asyncio.gather(
            *(one(case, repeat) for repeat in range(1, args.repetitions + 1) for case in selected)
        )
    metadata["complete"] = (
        len(metadata["results"]) == len(selected) * args.repetitions and not budget.exhausted
    )
    metadata["usage"] = budget.report()
    (output / "report.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "report": str((output / "report.json").resolve()),
                "complete": metadata["complete"],
                "usage": metadata["usage"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if metadata["complete"] else 1


def main():
    parser = argparse.ArgumentParser(description="Opt-in synthetic real LLM behavior evaluation")
    parser.add_argument(
        "--live", action="store_true", help="Explicit authorization to make real API calls"
    )
    parser.add_argument("--list-cases", action="store_true")
    parser.add_argument("--snapshot", type=Path, help="Save current prompts without model calls")
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--variant", default="baseline")
    parser.add_argument("--cases", default="all")
    parser.add_argument("--split", choices=["all", "dev", "holdout"], default="all")
    parser.add_argument("--repetitions", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--max-requests", type=int, default=256)
    parser.add_argument("--max-tokens", type=int, default=4000000)
    parser.add_argument("--max-cost", type=float)
    parser.add_argument("--input-price", type=float)
    parser.add_argument("--output-price", type=float)
    parser.add_argument("--model")
    parser.add_argument("--endpoint")
    parser.add_argument("--context-window", type=int)
    parser.add_argument("--concurrency", type=int, choices=[1, 2], default=2)
    parser.add_argument("--output", type=Path, default=Path(".heybuddy/evaluations"))
    args = parser.parse_args()
    if args.list_cases:
        print(
            json.dumps(
                [{"id": case.id, "split": case.split, "category": case.category} for case in CASES],
                ensure_ascii=False,
            )
        )
        return 0
    if args.snapshot:
        if args.snapshot.exists():
            parser.error("Snapshot already exists; preserve the baseline")
        prompts = capture(Path(".pytest_cache/evaluation-snapshot") / uuid4().hex)
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        args.snapshot.write_text(
            json.dumps(prompts, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "snapshot": str(args.snapshot),
                    "prompt_hash": digest_snapshot(prompts),
                    "corpus_hash": corpus_hash(),
                }
            )
        )
        return 0
    if not args.live:
        parser.error("Use --live to authorize paid API calls; otherwise --list-cases or --snapshot")
    if (
        args.max_requests < 1
        or args.max_tokens < 1
        or (args.max_cost is not None and args.max_cost <= 0)
    ):
        parser.error("Evaluation budgets must be positive")
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", args.variant):
        parser.error("Use a short portable variant label")
    try:
        settings = load_settings()
        updates = {
            key: value
            for key, value in {
                "model": args.model,
                "base_url": args.endpoint,
                "context_window": args.context_window,
            }.items()
            if value is not None
        }
        settings = type(settings).model_validate({**settings.model_dump(), **updates})
        prompts = (
            json.loads(args.prompt_file.read_text(encoding="utf-8"))
            if args.prompt_file
            else capture(Path(".pytest_cache/evaluation-snapshot") / uuid4().hex)
        )
        return asyncio.run(live(args, settings, prompts))
    except (ValidationError, ValueError, OSError):
        print("Evaluation configuration/storage error; check parameters.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
