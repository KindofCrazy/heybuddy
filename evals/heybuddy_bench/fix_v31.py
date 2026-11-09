"""Preserve v0.3; fix the temporal window omission found by its real Smoke trace."""

import json
from pathlib import Path

from revise_v3 import publish

from personal_agent.evaluation.heybuddy_bench.dataset import read_rows

ROOT = Path(__file__).resolve().parent


def main():
    cases = read_rows(ROOT / "v0_3/inputs.jsonl")
    gold = {row["case_id"]: row for row in read_rows(ROOT / "v0_3/gold.jsonl")}
    audit = []
    for case in cases:
        cid, body = case["case_id"], case["input"]
        if cid.startswith("custom_pd_") and gold[cid]["gold"]["action"] == "notify":
            for event in body["recent_user_evidence"]:
                if "临近时可以提醒我检查进度" in event["text"]:
                    event["text"] = event["text"].replace(
                        "临近时可以提醒我检查进度",
                        f"请在{body['now'][:10]}的16:00主动问我检查进度",
                    )
                    audit.append(dict(case_id=cid, reason="Explicit, dated local check window"))
    if not audit:
        raise ValueError("Window amendment must actually change at least one input")
    publish(ROOT / "v0_3_1", [(case, gold[case["case_id"]]) for case in cases])
    (ROOT / "v0_3_1/revisions.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


if __name__ == "__main__":
    main()
