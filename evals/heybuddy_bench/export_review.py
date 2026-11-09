"""Export a human review queue; refuse to overwrite completed reviewer fields."""

import argparse
import csv
import json
from pathlib import Path

from personal_agent.evaluation.heybuddy_bench.dataset import load_inputs, read_rows

ROOT = Path(__file__).resolve().parent


def export(directory: Path = ROOT):
    path = directory / "human_review.csv"
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as stream:
            if any(r.get("reviewer") for r in csv.DictReader(stream)):
                raise ValueError("Preserve existing human review; choose a separate export")
    gold = {r["case_id"]: r for r in read_rows(directory / "gold.jsonl")}
    fields = [
        "case_id",
        "track",
        "category",
        "input_excerpt",
        "gold_action",
        "acceptable_actions",
        "state_assertions",
        "evidence_present",
        "unambiguous",
        "natural_language",
        "gold_correct",
        "reviewer_note",
        "reviewer",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        cases = load_inputs(directory)
        for row in cases:
            body, track = row["input"], row["track"]
            excerpt = (
                body["current_message"]
                if track == "thread_routing"
                else " | ".join(
                    e["text"]
                    for e in (
                        body["episode"]["events"]
                        if track == "memory_learning"
                        else body["recent_user_evidence"]
                    )
                )
            )
            writer.writerow(
                dict(
                    case_id=row["case_id"],
                    track=track,
                    category=row["category"],
                    input_excerpt=excerpt[:400],
                    gold_action=gold[row["case_id"]]["gold"]["action"],
                    acceptable_actions=json.dumps(
                        gold[row["case_id"]]["gold"].get("acceptable_actions", []),
                        ensure_ascii=False,
                    ),
                    state_assertions=json.dumps(
                        gold[row["case_id"]]["gold"].get("fact_assertions", []),
                        ensure_ascii=False,
                    ),
                )
            )
    print(f"Exported {len(cases)} review rows; full context remains in inputs.jsonl/gold.jsonl")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=ROOT)
    export(parser.parse_args().dataset)
