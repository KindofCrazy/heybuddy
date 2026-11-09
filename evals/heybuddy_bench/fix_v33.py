"""Freeze the policy that tomorrow's internal review does not contact the user today."""

import json
from datetime import datetime, timedelta
from pathlib import Path

from revise_v3 import publish

from personal_agent.evaluation.heybuddy_bench.dataset import read_rows, sha256

ROOT = Path(__file__).resolve().parent


def main():
    for source, destination in [("v0_3_2", "v0_3_3"), ("v0_3_2_challenge", "v0_3_3_challenge")]:
        cases = read_rows(ROOT / source / "inputs.jsonl")
        gold = {r["case_id"]: r for r in read_rows(ROOT / source / "gold.jsonl")}
        audit = []
        if source == "v0_3_2":
            for number in [7, 8, 28, 29]:
                cid = f"custom_pd_{number:03}"
                label = gold[cid]["gold"]
                label["acceptable_actions"] = ["silent", "defer"]
                floor = datetime.fromisoformat(label["as_of"])
                if number in {28, 29}:
                    floor = (floor + timedelta(days=1)).replace(hour=0, minute=0, second=0)
                label["review_not_before"] = floor.isoformat()
                audit.append(
                    dict(
                        case_id=cid,
                        reason="Later internal review is not immediate contact",
                        review_not_before=floor.isoformat(),
                    )
                )
        publish(ROOT / destination, [(c, gold[c["case_id"]]) for c in cases])
        path = ROOT / destination / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest.update(
            version="0.3.3",
            total=len(cases),
            temporal_scorer_sha256=sha256(
                Path("src/personal_agent/evaluation/heybuddy_bench/temporal_scoring.py")
            ),
            fact_scorer_sha256=sha256(
                Path("src/personal_agent/evaluation/heybuddy_bench/fact_scoring.py")
            ),
        )
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
        (ROOT / destination / "revisions.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )


if __name__ == "__main__":
    main()
