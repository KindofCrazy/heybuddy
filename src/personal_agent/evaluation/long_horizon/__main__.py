"""Offline-only CLI; intentionally has no live provider construction path."""

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path

from personal_agent.evaluation.long_horizon.dataset import load
from personal_agent.evaluation.long_horizon.models import Config
from personal_agent.evaluation.long_horizon.report import default_output, evaluate, recompute


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--mode", choices=["routing"], default="routing")
    parser.add_argument("--variant", choices=["rule"], default="rule")
    parser.add_argument("--replay-mode", choices=["streaming"], default="streaming")
    parser.add_argument("--dataset", type=Path, default=Path("evals/long_horizon"))
    parser.add_argument("--scenario")
    parser.add_argument("--split", default="dev")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        if args.report:
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
            result = asyncio.run(
                evaluate(dataset, output, split=args.split, scenario_id=args.scenario)
            )
            print(json.dumps({"result_dir": str(output)}))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"status": "failed", "error_code": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
