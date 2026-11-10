"""Export body-free counts from an existing private experiment; never calls a model."""

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(".heybuddy/evaluations/sequence_20261110")
    )
    parser.add_argument(
        "--output", type=Path, default=Path(__file__).parent / "results" / "initial_dev.json"
    )
    args = parser.parse_args()
    report = json.loads((args.root / "live-dev/report.json").read_text(encoding="utf-8"))
    smoke = json.loads((args.root / "live-smoke/report.json").read_text(encoding="utf-8"))
    ledger = json.loads((args.root / "session.json").read_text(encoding="utf-8"))
    if ledger["pending"] or not ledger["usage_complete"]:
        raise ValueError("Cannot publish a completed cost with pending or unknown usage")
    if report["hashes"] != smoke["hashes"]:
        raise ValueError("Smoke and dev implementations or data differ")
    families = {}
    failures = []
    for item in report["details"]:
        family = families.setdefault(
            item["track"] + "/" + item["family"], dict(scenarios=0, passed=0)
        )
        family["scenarios"] += 1
        family["passed"] += int(item["passed"])
        for check in item["checks"]:
            if not check["passed"]:
                failures.append(
                    dict(
                        case_id=item["case_id"],
                        step=check["step"],
                        failed_checks=dict(
                            Counter(
                                k.split(":", 1)[0]
                                for k, value in check["assertions"].items()
                                if not value
                            )
                        ),
                    )
                )
    summary = dict(
        exported_at=datetime.now(UTC).isoformat(),
        dataset="sequence-1.0",
        human_reviewed=False,
        model="deepseek-flash",
        split="dev",
        expected=report["expected"],
        observed=report["observed"],
        tracks=report["tracks"],
        families=families,
        failed_checkpoints=failures,
        execution_error_cases=report["errors"],
        hashes=report["hashes"],
        protocol_hash=ledger["protocol_hash"],
        cumulative_usage=ledger,
        dev_requests=ledger["requests"] - smoke["usage"]["requests"],
        smoke_requests=smoke["usage"]["requests"],
        holdout_observed=0,
        configuration=report["configuration"],
        limitations=[
            "Agent-authored synthetic Gold; no human review or representative sampling.",
            "Variants share scenario families; checkpoints and variants are correlated.",
            "State checks are lexical/structural, not full semantic correctness or user utility.",
            "Memory/Attention checkpoints are controlled; no autonomous scheduler benchmark.",
            "Routing history visibility is a proxy; no downstream Agent task success measured.",
            "Production policies and scorer unchanged after the initial live freeze.",
            "Smoke repeats 3 development scenarios; cumulative usage includes both stages.",
            "Cost is a cache-miss peak-price upper estimate, not the provider's final invoice.",
        ],
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(
        json.dumps(
            dict(
                output=str(args.output),
                requests=ledger["requests"],
                cost_cny_upper_estimate=ledger["cost_cny_upper_estimate"],
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
