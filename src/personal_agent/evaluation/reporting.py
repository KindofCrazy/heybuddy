"""Offline paired comparisons from synthetic traces; never exports response text or evidence."""

import argparse
import json
from pathlib import Path

from personal_agent.evaluation.cases import CASES
from personal_agent.evaluation.prompts import digest_snapshot
from personal_agent.evaluation.scoring import score

AUTOMATION_STAGES = {"worthiness", "extract", "select", "consolidate"}


def ratio(numerator: int, denominator: int) -> dict:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": numerator / denominator if denominator else None,
    }


def indicators(observations: list[dict]) -> dict:
    cases = {case.id: case for case in CASES}
    direct = [row for row in observations if cases[row["case"]].expected.get("max_tools") == 0]
    negative = [
        row for row in observations if cases[row["case"]].expected.get("memory") == "negative"
    ]
    retrieval = [row for row in observations if "read" in cases[row["case"]].expected]
    updates = [row for row in observations if cases[row["case"]].expected.get("memory") == "update"]
    proposals = [
        (row, item)
        for row in observations
        for item in row["items"]
        if item["action"] in {"create", "update"}
    ]
    return {
        "unnecessary_tool_run_rate_on_zero_tool_cases": ratio(
            sum(bool(row["tools"]) for row in direct), len(direct)
        ),
        "false_write_proposal_rate_on_negative_cases": ratio(
            sum(any(i["action"] in {"create", "update"} for i in row["items"]) for row in negative),
            len(negative),
        ),
        "negative_decision_failures": sum(
            row["automation_outcome"] != "not_worthy" for row in negative
        ),
        "target_retrieval_accuracy": ratio(
            sum(row["grading"]["checks"]["retrieval"] for row in retrieval), len(retrieval)
        ),
        "update_contract_success": ratio(
            sum(row["grading"]["pass"] for row in updates), len(updates)
        ),
        "proposal_action_target_precision_proxy": ratio(
            sum(
                item["action"] == "update"
                and item["path"] == cases[row["case"]].expected.get("target")
                for row, item in proposals
            ),
            len(proposals),
        ),
        "semantic_reflection_precision": None,
    }


def metrics(observations: list[dict], input_price: float, output_price: float) -> dict:
    calls = [call for row in observations for call in row["calls"]]
    stages = {}
    for name, selected in {
        "main": [c for c in calls if c["stage"] == "main"],
        "automation": [c for c in calls if c["stage"] in AUTOMATION_STAGES],
        "compaction": [c for c in calls if c["stage"] == "compaction"],
    }.items():
        known = [c["usage"] for c in selected if c.get("usage") is not None]
        incoming = sum(c["input_tokens"] for c in known)
        outgoing = sum(c["output_tokens"] for c in known)
        stages[name] = {
            "requests": len(selected),
            "input_tokens": incoming,
            "output_tokens": outgoing,
            "reasoning_tokens": sum(c.get("reasoning_tokens", 0) or 0 for c in known),
            "usage_complete": len(known) == len(selected),
            "seconds": round(sum(c["seconds"] for c in selected), 3),
            "estimated_usd": (incoming * input_price + outgoing * output_price) / 1e6,
        }
    return {
        "runs": len(observations),
        "passed": sum(row["grading"]["pass"] for row in observations),
        "case_seconds_sum": round(sum(row["seconds"] for row in observations), 3),
        "indicators": indicators(observations),
        "stages": stages,
        "failed_checks": {
            key: sum(not row["grading"]["checks"].get(key, True) for row in observations)
            for key in sorted({key for row in observations for key in row["grading"]["checks"]})
        },
    }


def load_run(folder: Path, corpus: str) -> tuple[dict, dict]:
    report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    prompts = json.loads((folder / "prompts.json").read_text(encoding="utf-8"))
    if not report.get("complete") or report["corpus_hash"] != corpus:
        raise ValueError("Incomplete run or changed corpus")
    if report["prompt_hash"] != digest_snapshot(prompts):
        raise ValueError("Changed prompt snapshot")
    expected = {
        (case, repeat)
        for case in report["case_ids"]
        for repeat in range(1, report["repetitions"] + 1)
    }
    rows = {}
    cases = {case.id: case for case in CASES}
    for row in report["results"]:
        key = (row["case"], row["repeat"])
        if key in rows or key not in expected:
            raise ValueError("Duplicate or unexpected case result")
        observation = json.loads(
            (folder / f"{key[0]}-{key[1]}" / "result.json").read_text(encoding="utf-8")
        )
        if score(cases[key[0]], observation) != observation["grading"]:
            raise ValueError("Scoring changed after collection")
        if row["pass"] != observation["grading"]["pass"]:
            raise ValueError("Result disagrees with trace")
        rows[key] = observation
    if set(rows) != expected:
        raise ValueError("Missing results")
    return report, rows


def campaign(root: Path, manifest: dict) -> dict:
    runs = {}
    settings = None
    for path in sorted(root.glob("run-*/report.json")):
        report, rows = load_run(path.parent, manifest["corpus_hash"])
        signature = {
            key: report[key] for key in ("model", "endpoint", "context_window", "parameters")
        }
        if settings is not None and settings != signature:
            raise ValueError("Incomparable model/parameters")
        settings = signature
        if report["variant"] in runs:
            raise ValueError("Ambiguous variant; provide a root containing one run per variant")
        runs[report["variant"]] = (report, rows)
    if not runs:
        raise ValueError("No collected runs")
    prices = manifest["input_usd_per_million"], manifest["output_usd_per_million"]
    output = {"settings": settings, "corpus_hash": manifest["corpus_hash"], "runs": {}}
    for name, (report, rows) in runs.items():
        output["runs"][name] = {
            "prompt_hash": report["prompt_hash"],
            "usage": report["usage"],
            **metrics(list(rows.values()), *prices),
        }
    pairs = {}
    for candidate, (_report, rows) in runs.items():
        if candidate.startswith("baseline"):
            continue
        baseline = "baseline-holdout" if "holdout" in candidate else "baseline-dev"
        baseline_report, base_rows = runs[baseline]
        if baseline_report["prompt_hash"] != manifest["baseline_prompt_hash"]:
            raise ValueError("Wrong Phase 1 baseline")
        if not rows.keys() <= base_rows.keys():
            raise ValueError("Unpaired observations")
        pairs[candidate] = {
            "baseline": metrics([base_rows[key] for key in rows], *prices),
            "candidate": metrics(list(rows.values()), *prices),
            "changes": [
                {
                    "case": key[0],
                    "repeat": key[1],
                    "baseline_pass": base_rows[key]["grading"]["pass"],
                    "candidate_pass": row["grading"]["pass"],
                }
                for key, row in sorted(rows.items())
                if base_rows[key]["grading"]["pass"] != row["grading"]["pass"]
            ],
        }
    output["paired"] = pairs
    usage = [report["usage"] for report, _ in runs.values()]
    output["campaign"] = {
        key: sum(row[key] for row in usage)
        for key in (
            "requests",
            "reserved_tokens",
            "reserved_cost_usd",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "estimated_cost_usd",
        )
    }
    if (
        output["campaign"]["requests"] > manifest["max_campaign_requests"]
        or output["campaign"]["reserved_tokens"] > manifest["max_campaign_reserved_tokens"]
        or output["campaign"]["reserved_cost_usd"] > manifest["max_campaign_estimated_usd"]
    ):
        raise ValueError("Campaign exceeded its predeclared budget")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(".heybuddy/evaluations"))
    parser.add_argument("--manifest", type=Path, default=Path("evals/manifest.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from personal_agent.evaluation.__main__ import corpus_hash

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest["corpus_hash"] != corpus_hash():
        raise ValueError("Current corpus differs from the frozen manifest")
    result = campaign(args.root, manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["campaign"]))


if __name__ == "__main__":
    main()
