"""Re-score saved live decisions without new calls or access from the task model to gold."""

import argparse
import copy
import json
from pathlib import Path

from personal_agent.evaluation.heybuddy_bench.dataset import load_inputs, read_rows, sha256
from personal_agent.evaluation.heybuddy_bench.experiment import (
    cny,
    production_code_hash,
    reviewed_score,
    write_json,
)
from personal_agent.evaluation.heybuddy_bench.scoring import aggregate


def repair_observation(row: dict) -> dict:
    """Repair the v0.2 cross-case *reporting* bug from per-case calls and published effects.

    Completed writes are evidenced by their actual changes/receipts. An empty Extraction is
    a no-op. A completed raw create with no effect was held for review (duplicate topic);
    uncertain writes are likewise review. Equal-content updates are no-ops. No text, target,
    model output, file effect or failed outcome is changed. Original artifacts stay intact.
    """
    row = copy.deepcopy(row)
    prediction = row["prediction"]
    if row["track"] != "memory_learning" or "model_decisions" in prediction:
        return row
    decisions = [
        call["parsed"]
        for call in row["calls"]
        if call["schema"] in {"FileDecision", "ExistingFileDecision"} and call.get("parsed")
    ]
    prediction["model_decisions"] = decisions
    prediction["decisions"] = decisions
    if prediction.get("outcome") == "completed" and not prediction["changes"]:
        action = decisions[0]["action"] if decisions else "no_op"
        if action in {"create", "update"}:
            extraction = (
                next((c.get("parsed") for c in row["calls"] if c["schema"] == "Extraction"), {})
                or {}
            )
            uncertain = any(
                c["confidence"] == "uncertain" for c in extraction.get("candidates", [])
            )
            action = "review" if action == "create" or uncertain else "no_op"
        prediction["action"] = action
    prediction["observation_repair"] = "Per-case calls and persisted effects; no model rerun"
    return row


def summarize(cases, gold, saved_rows, protocol):
    rows = [repair_observation(r) for r in saved_rows]
    # Legacy runner arithmetic used USD field names; this protocol uses verified CNY prices.
    for row in rows:
        usage = row["usage"]
        usage["api_cost_usd"] = usage.get("cost_cny_upper_estimate", usage.get("api_cost_usd"))
    report = cny(aggregate(cases, gold, rows))
    by_id = {row["case_id"]: row for row in rows}
    for detail in report["cases"]:
        row = by_id.get(detail["case_id"])
        if row is None:
            continue
        prediction = row["prediction"]
        detail.update(
            predicted_targets=(
                [change["path"] for change in prediction.get("changes", [])]
                if detail["track"] == "memory_learning"
                else prediction.get("proposal_ids", [])
                if detail["track"] == "proactive_decision"
                else [prediction.get("thread_id")]
            ),
            execution_error=prediction.get("error_code")
            or prediction.get("runtime", {}).get("error_code"),
            usage=cny({k: v for k, v in row["usage"].items() if k != "api_cost_usd"}),
        )
    report["reviewed"] = reviewed_score(cases, gold, rows, protocol)
    for track, result in report["tracks"].items():
        details = [r for r in report["reviewed"] if r["track"] == track]
        result["reviewed_decision_accuracy"] = sum(r["reviewed_correct"] for r in details) / len(
            details
        )
        result["execution_errors"] = sum(
            r["prediction"]["action"] == "__error__" for r in rows if r["track"] == track
        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=Path("evals/heybuddy_bench"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--freeze-candidate", action="store_true")
    parser.add_argument("--candidate-tag", choices=["candidate", "final"], default="candidate")
    args = parser.parse_args()
    protocol_path = args.dataset / "live_protocol.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    freeze = args.root / (args.candidate_tag + "-freeze.json")
    if args.freeze_candidate:
        if freeze.exists():
            raise ValueError("Candidate freeze already exists")
        write_json(
            freeze,
            dict(
                production_code_hash=production_code_hash(),
                protocol_hash=sha256(protocol_path),
                selection=(
                    "Development evidence only; before holdout inspection"
                    if args.candidate_tag == "candidate"
                    else "Development format fixes and offline compatibility; reused holdout"
                ),
            ),
        )
        print("Candidate frozen")
        return
    frozen = json.loads(freeze.read_text(encoding="utf-8"))
    if frozen["production_code_hash"] != production_code_hash():
        raise ValueError("Candidate changed after freeze")
    cases = load_inputs(args.dataset)
    gold = read_rows(args.dataset / "gold.jsonl")
    reports = {}
    stages = {}
    for arm in ("original", args.candidate_tag):
        combined = []
        complete = True
        for subset, key in (("dev", "development"), ("holdout", "holdout")):
            stage = arm + "-" + subset
            path = args.root / stage / "predictions.jsonl"
            ids = set(protocol[key])
            if not path.exists():
                complete = False
                reports[stage] = dict(status="not_run", expected=len(ids))
                continue
            rows = read_rows(path)
            stages[stage] = dict(
                provenance=json.loads(
                    (path.parent / "provenance.json").read_text(encoding="utf-8")
                ),
                predictions_sha256=sha256(path),
            )
            combined.extend(rows)
            reports[stage] = summarize(
                [c for c in cases if c["case_id"] in ids],
                [g for g in gold if g["case_id"] in ids],
                rows,
                protocol,
            )
        if complete:
            reports[arm + "-all"] = summarize(cases, gold, combined, protocol)
    result = dict(
        protocol_hash=sha256(protocol_path),
        candidate=frozen,
        annotation_status=protocol["annotation_status"],
        ledger=json.loads((args.root / "session.json").read_text()),
        stages=stages,
        reports=reports,
    )
    if not args.output:
        raise ValueError("Specify a summary output path")
    write_json(args.output, result)
    print(
        json.dumps(
            {
                stage: (
                    {k: round(v["decision_accuracy"], 4) for k, v in report["tracks"].items()}
                    if "tracks" in report
                    else report
                )
                for stage, report in reports.items()
            }
        )
    )


if __name__ == "__main__":
    main()
