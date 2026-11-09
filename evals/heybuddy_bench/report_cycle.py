"""Export text-free actual stage reports, costs and trace hashes; no inference or gold edits."""

import argparse
import hashlib
import json
from pathlib import Path

from personal_agent.evaluation.heybuddy_bench.cycle import selected_cases
from personal_agent.evaluation.heybuddy_bench.dataset import sha256
from personal_agent.evaluation.heybuddy_bench.experiment import write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(".heybuddy/evaluations/hb-cycle-20261108-v3")
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--baseline-root", type=Path, default=Path(".heybuddy/evaluations/hb-cycle-baseline-v4")
    )
    args = parser.parse_args()
    stages, costs = {}, {}
    for path in sorted(args.root.glob("*/report.json")):
        stage = path.parent.name
        report = json.loads(path.read_text(encoding="utf-8"))
        provenance = json.loads((path.parent / "provenance.json").read_text(encoding="utf-8"))
        prediction_path = path.parent / "predictions.jsonl"
        rows = [
            json.loads(line) for line in prediction_path.read_text(encoding="utf-8").splitlines()
        ]
        totals = {
            key: (
                sum(r["usage"][key] for r in rows)
                if all(r["usage"][key] is not None for r in rows)
                else None
            )
            for key in (
                "api_requests",
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cost_cny_upper_estimate",
            )
        }
        costs[stage] = totals
        cases = selected_cases(provenance["part"], provenance.get("revision", "v0_3"))
        if provenance.get("track_filter"):
            cases = [c for c in cases if c["track"] == provenance["track_filter"]]
        hashes = {
            track: hashlib.sha256(
                json.dumps(
                    [
                        {"case_id": c["case_id"], "input": c["input"]}
                        for c in cases
                        if c["track"] == track
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            for track in {c["track"] for c in cases}
        }
        # Old preliminary Smoke records are retained but never exported as capability scores.
        stages[stage] = dict(
            provenance=provenance,
            task_input_hashes_by_track=hashes,
            predictions_sha256=sha256(prediction_path),
            expected=report["expected"],
            observed=report["observed"],
            reports=report["reports"] if report["part"] != "smoke" else {},
            errors=[r["case_id"] for r in rows if r["prediction"]["action"] == "__error__"],
        )
    rescored = {}
    for path in sorted(args.root.glob("*-v33-final.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        # Re-scoring changed labels, not the actual model outputs or billed usage.
        rescored[path.stem] = report
    ledger = json.loads((args.root / "session.json").read_text(encoding="utf-8"))
    result = dict(
        annotation="Agent reviewed; no independent human review; reserved scenarios are correlated",
        historical_cost_cny=1.875786,
        round_budget_cny=20,
        round_cost_cny_upper_estimate=ledger["cost_cny_upper_estimate"] - 1.875786,
        ledger=ledger,
        baseline={
            p.parent.name: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(args.baseline_root.glob("*/report.json"))
        },
        costs=costs,
        stages=stages,
        rescored=rescored,
    )
    write_json(args.output, result)
    print(
        json.dumps(
            dict(
                stages=len(stages),
                requests=ledger["requests"] - 565,
                round_cost_cny=result["round_cost_cny_upper_estimate"],
            )
        )
    )


if __name__ == "__main__":
    main()
