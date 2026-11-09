"""Re-score immutable trajectories only when all task inputs are unchanged; zero API calls."""

import argparse
import json
from pathlib import Path

from personal_agent.evaluation.heybuddy_bench.cycle import DATA, selected_cases, summarize
from personal_agent.evaluation.heybuddy_bench.dataset import read_rows, sha256, validate
from personal_agent.evaluation.heybuddy_bench.experiment import write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--revision", choices=["v0_3_2", "v0_3_3"], default="v0_3_2")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Preserve previous summaries")
    provenance = json.loads((args.source / "provenance.json").read_text(encoding="utf-8"))
    part = provenance["part"]
    previous = selected_cases(part, provenance.get("revision", "v0_3"))
    current = selected_cases(part, args.revision)
    if {c["case_id"]: c["input"] for c in previous} != {c["case_id"]: c["input"] for c in current}:
        raise ValueError("Task inputs changed; saved inference cannot be reused")
    validate(DATA / args.revision)
    validate(DATA / (args.revision + "_challenge"), expected=10)
    rows = read_rows(args.source / "predictions.jsonl")
    for row in rows:
        row["usage"]["api_cost_usd"] = row["usage"]["cost_cny_upper_estimate"]
    result = dict(
        source=str(args.source),
        source_provenance=provenance,
        source_predictions_sha256=sha256(args.source / "predictions.jsonl"),
        revision=args.revision,
        new_api_requests=0,
        new_cost_cny=0,
        data_hashes={
            f"{folder}/{name}": sha256(DATA / folder / name)
            for folder in [args.revision, args.revision + "_challenge"]
            for name in ["inputs.jsonl", "gold.jsonl", "manifest.json"]
        },
        reports=summarize(current, rows, args.revision),
    )
    write_json(args.output, result)
    print(
        json.dumps(
            {
                group: {t: v["decision_accuracy"] for t, v in report["tracks"].items()}
                for group, report in result["reports"].items()
            }
        )
    )


if __name__ == "__main__":
    main()
