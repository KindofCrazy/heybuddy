"""Offline by default; bounded model smoke requires explicit --live."""

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from personal_agent.evaluation.long_horizon.dataset import load
from personal_agent.evaluation.long_horizon.models import Config
from personal_agent.evaluation.long_horizon.report import default_output, evaluate, recompute


async def run_live(args, dataset, output):
    from openai import AsyncOpenAI

    from personal_agent.config import load_settings
    from personal_agent.evaluation.long_horizon.live import (
        DevLiveConfig,
        LiveConfig,
        NoThinkingDevConfig,
        evaluate_live,
        validate_live_scope,
    )
    from personal_agent.llm.openai_provider import OpenAIProvider

    settings = load_settings()  # Normal configuration loader; never dump secrets/settings.
    kind = {
        "smoke": LiveConfig,
        "routing-dev-v1": DevLiveConfig,
        "routing-dev-v1-no-thinking": NoThinkingDevConfig,
    }[args.budget_profile]
    overrides = {
        k: getattr(args, k)
        for k in ("repetitions", "max_requests", "max_tokens", "max_cost_cny")
        if getattr(args, k) is not None
    }
    config = kind(
        model=settings.model,
        endpoint_host=urlsplit(str(settings.base_url or "https://api.openai.com")).hostname,
        context_window=settings.context_window,
        **overrides,
        input_price_cny=args.input_price_cny,
        output_price_cny=args.output_price_cny,
        price_source=args.price_source,
    )
    validate_live_scope(dataset, config, args.split, args.scenario)
    async with AsyncOpenAI(
        api_key=settings.api_key.get_secret_value(),
        base_url=str(settings.base_url) if settings.base_url else None,
        max_retries=0,
        timeout=10,
    ) as client:
        return await evaluate_live(
            dataset,
            output,
            OpenAIProvider(client, settings.model),
            config,
            split=args.split,
            scenario_id=args.scenario,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--planning-input-cap", type=int, default=4096)
    parser.add_argument("--mode", choices=["routing"], default="routing")
    parser.add_argument("--variant", choices=["rule", "model-assisted"], default="rule")
    parser.add_argument("--replay-mode", choices=["streaming"], default="streaming")
    parser.add_argument("--dataset", type=Path, default=Path("evals/long_horizon"))
    parser.add_argument("--scenario")
    parser.add_argument("--split", default="dev")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--budget-profile",
        choices=["smoke", "routing-dev-v1", "routing-dev-v1-no-thinking"],
        default="smoke",
    )
    parser.add_argument("--repetitions", type=int)
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--max-cost-cny", type=float)
    parser.add_argument("--input-price-cny", type=float)
    parser.add_argument("--output-price-cny", type=float)
    parser.add_argument("--price-source")
    args = parser.parse_args()
    if args.budget_profile != "smoke" and not args.live:
        parser.error("Explicit dev budget requires --live")
    if args.preflight and (args.live or args.report or args.list or args.variant != "rule"):
        parser.error("--preflight is offline-only and cannot list/re-score/run live")
    if args.live and (args.variant != "model-assisted" or args.list or args.report):
        parser.error("--live requires --variant model-assisted and cannot list/re-score")
    if args.variant == "model-assisted" and not args.live:
        parser.error("Model requests require explicit --live")
    if args.live and any(
        value is None for value in (args.input_price_cny, args.output_price_cny, args.price_source)
    ):
        parser.error("Live smoke requires verified CNY input/output prices and a source")
    try:
        if args.report:
            if (args.report / "live-trace.json").is_file():
                from personal_agent.evaluation.long_horizon.live import recompute_live

                result = recompute_live(args.report)
            else:
                result = recompute(args.report)
        else:
            dataset = replace(load(args.dataset), config=Config(seed=args.seed))
            if args.list:
                print(
                    json.dumps(
                        {
                            "evaluation_type": "Offline Contract Test",
                            "scenarios": [
                                s.scenario_id for s in dataset.select(args.split, args.scenario)
                            ],
                        }
                    )
                )
                return 0
            output = args.output or default_output()
            if args.preflight:
                from personal_agent.evaluation.long_horizon.preflight import preflight
                from personal_agent.evaluation.long_horizon.report import (
                    evaluation_code_hash,
                    prepare_output,
                    working_tree_dirty,
                    write,
                )

                revision = prepare_output(output)
                try:
                    result = asyncio.run(
                        preflight(
                            dataset,
                            split=args.split,
                            scenario_id=args.scenario,
                            input_cap=args.planning_input_cap,
                        )
                    )
                    result.update(
                        git_commit=revision.stdout.strip() if revision.returncode == 0 else None,
                        git_dirty=working_tree_dirty(),
                        code_hash=evaluation_code_hash(),
                    )
                    write(output / "preflight.json", result)
                    write(output / "status.json", {"status": "completed"})
                except BaseException:
                    write(output / "status.json", {"status": "failed"})
                    raise
                print(json.dumps({"result_dir": str(output), **result}, ensure_ascii=False))
                return 0
            result = asyncio.run(
                run_live(args, dataset, output)
                if args.live
                else evaluate(dataset, output, split=args.split, scenario_id=args.scenario)
            )
            print(json.dumps({"result_dir": str(output)}))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"status": "failed", "error_code": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
