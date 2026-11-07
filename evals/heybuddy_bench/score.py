"""Recompute a complete-denominator report from saved runner predictions."""

import argparse
import json
from pathlib import Path

from personal_agent.evaluation.heybuddy_bench.dataset import load_inputs, read_rows
from personal_agent.evaluation.heybuddy_bench.scoring import aggregate

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("predictions", type=Path)
    parser.add_argument("--dataset", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    print(
        json.dumps(
            aggregate(
                load_inputs(args.dataset),
                read_rows(args.dataset / "gold.jsonl"),
                read_rows(args.predictions),
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
