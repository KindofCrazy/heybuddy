"""Frozen, family-split sequential fixtures and deterministic diagnostic assertions."""

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

TRACKS = ("routing", "memory", "proactive")
DATA = Path("evals/heybuddy_bench/sequence_v1")


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate(path=DATA):
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    for name, checksum in manifest["sha256"].items():
        if sha(path / name) != checksum:
            raise ValueError("Frozen sequence checksum mismatch")
    cases, gold = rows(path / "inputs.jsonl"), rows(path / "gold.jsonl")
    identifiers = [c["case_id"] for c in cases]
    if len(set(identifiers)) != len(cases) or identifiers != [g["case_id"] for g in gold]:
        raise ValueError("Duplicate or unaligned sequence IDs")
    families = defaultdict(set)
    for case, answer in zip(cases, gold, strict=True):
        if set(case) != {"case_id", "track", "family", "split", "input"}:
            raise ValueError("Unexpected sequence metadata")
        track, body = case["track"], case["input"]
        if track not in TRACKS or case["split"] not in {"dev", "holdout"}:
            raise ValueError("Unknown track or split")
        families[(track, case["family"])].add(case["split"])
        allowed = {
            "routing": {"threads", "active", "history", "steps"},
            "memory": {"memory_files", "steps"},
            "proactive": {"steps"},
        }[track]
        if set(body) != allowed:
            raise ValueError("Answer-bearing or unknown input field")
        steps = body["steps"]
        if not 1 <= len(steps) <= 8 or len(steps) != len(answer["checks"]):
            raise ValueError("Invalid checkpoint count")
        if len({s["id"] for s in steps}) != len(steps):
            raise ValueError("Duplicate step ID")
        times = []
        for step in steps:
            if set(step) - {"id", "at", "text", "assistant", "learn", "attention"}:
                raise ValueError("Answer-bearing or unknown step field")
            at = datetime.fromisoformat(step["at"])
            if at.tzinfo is None:
                raise ValueError("Naive sequence time")
            times.append(at)
        if times != sorted(times):
            raise ValueError("Sequence contains time travel")
        for i, check in enumerate(answer["checks"]):
            disclosed = {s["id"] for s in steps[: i + 1] if s.get("text")}
            for opportunity in check.get("opportunities", []):
                if set(opportunity["evidence"]) - disclosed:
                    raise ValueError("Gold references future or missing evidence")
        if track == "routing":
            tids = [t["id"] for t in body["threads"]]
            if len(set(tids)) != len(tids) or body["active"] not in tids:
                raise ValueError("Invalid Thread inventory")
            for card in body["threads"]:
                if set(card) != {"id", "title", "current_state", "last_active_at"}:
                    raise ValueError("Unknown Thread field")
            for event in body["history"]:
                if set(event) != {"thread_id", "text", "timestamp"}:
                    raise ValueError("Unknown history field")
                if (
                    event["thread_id"] not in tids
                    or datetime.fromisoformat(event["timestamp"]) > times[0]
                ):
                    raise ValueError("Invalid historical evidence")
    if any(len(splits) != 1 for splits in families.values()):
        raise ValueError("Scenario family crosses dev/holdout")
    if len(cases) != manifest["total"] or len(families) != manifest["families"]:
        raise ValueError("Manifest count mismatch")
    splits = dict(Counter(c["split"] for c in cases))
    if splits != manifest["split"]:
        raise ValueError("Manifest split mismatch")
    return dict(
        total=len(cases),
        families=len(families),
        splits=splits,
        checkpoints=sum(len(c["input"]["steps"]) for c in cases),
        human_reviewed=False,
    )


def contains(text, terms):
    return any(term.casefold() in text.casefold() for term in terms)


def check_step(track, expected, observed):
    """Checks are diagnostics, NOT semantic claim entailment or human preference labels."""
    tests = {}
    if not observed or observed.get("error_code"):
        return {"execution": False}
    if track == "routing":
        tests["action"] = observed["action"] in expected["actions"]
        if expected.get("target"):
            tests["target"] = observed["thread_id"] == expected["target"]
        for term in expected.get("context_has", []):
            tests[f"context_has:{term}"] = contains(observed["context"], [term])
        for term in expected.get("context_absent", []):
            tests[f"context_absent:{term}"] = not contains(observed["context"], [term])
    elif track == "memory":
        text = "\n".join(observed["files"].values())
        for i, choices in enumerate(expected.get("present", [])):
            tests[f"present:{i}"] = contains(text, choices)
        for term in expected.get("absent", []):
            tests[f"absent:{term}"] = not contains(text, [term])
        if item := expected.get("max_matching_files"):
            tests["duplicate_files"] = (
                sum(contains(t, [item["term"]]) for t in observed["files"].values())
                <= item["count"]
            )
    else:
        candidates = observed["proposals"]
        if "max_candidates" in expected:
            tests["candidate_count"] = len(candidates) <= expected["max_candidates"]
        if "gate" in expected:
            tests["gate"] = observed["gate"] == expected["gate"]
        if "actions" in expected:
            tests["action"] = observed["action"] in expected["actions"]
        if "selected_any" in expected:
            tests["selection"] = contains(observed["selected_text"], expected["selected_any"])
        for i, item in enumerate(expected.get("opportunities", [])):
            matches = [
                p
                for p in candidates
                if contains(p["title"] + " " + p["context"], item["terms"])
                and set(p["event_ids"]) & set(item["evidence"])
            ]
            tests[f"opportunity:{i}"] = len(matches) == 1
            tests[f"state:{i}"] = len(matches) == 1 and matches[0]["status"] in item["states"]
            if "followups" in item:
                tests[f"followup:{i}"] = (
                    len(matches) == 1 and matches[0]["followup_state"] in item["followups"]
                )
    return tests


def score(cases, gold, predictions):
    by_id = {r["case_id"]: r for r in predictions}
    if len(by_id) != len(predictions):
        raise ValueError("Duplicate predictions")
    answers = {g["case_id"]: g["checks"] for g in gold}
    details, tracks = [], {}
    for case in cases:
        prediction = by_id.get(case["case_id"], {})
        observed = prediction.get("steps", [])
        checks = []
        for i, expected in enumerate(answers[case["case_id"]]):
            actual = observed[i] if i < len(observed) else None
            tests = check_step(case["track"], expected, actual)
            item = dict(
                step=case["input"]["steps"][i]["id"],
                passed=bool(tests) and all(tests.values()),
                assertions=tests,
            )
            if case["track"] == "routing" and expected.get("target"):
                target = expected["target"]
                item["candidate_recalled"] = bool(actual and target in actual.get("candidates", []))
                item["effective_candidate_recalled"] = bool(
                    actual and target in actual.get("effective_candidates", [])
                )
                item["model_candidates_recalled"] = (
                    target in actual["model_candidates"]
                    if actual and actual.get("model_candidates") is not None
                    else None
                )
            checks.append(item)
        details.append(
            dict(
                case_id=case["case_id"],
                track=case["track"],
                family=case["family"],
                passed=all(c["passed"] for c in checks) and not prediction.get("error_code"),
                checks=checks,
                error_code=prediction.get("error_code"),
            )
        )
    for track in TRACKS:
        selected = [d for d in details if d["track"] == track]
        checkpoints = [s for d in selected for s in d["checks"]]
        recalled = [c["candidate_recalled"] for c in checkpoints if "candidate_recalled" in c]
        model_recalled = [
            c["model_candidates_recalled"]
            for c in checkpoints
            if c.get("model_candidates_recalled") is not None
        ]
        tracks[track] = dict(
            scenarios=len(selected),
            scenarios_passed=sum(d["passed"] for d in selected),
            checkpoints=len(checkpoints),
            checkpoints_passed=sum(c["passed"] for c in checkpoints),
            families=len({d["family"] for d in selected}),
        )
        if recalled:
            tracks[track]["recent_candidate_recall"] = dict(hits=sum(recalled), total=len(recalled))
            effective = [
                c["effective_candidate_recalled"]
                for c in checkpoints
                if "effective_candidate_recalled" in c
            ]
            tracks[track]["effective_candidate_recall_with_active"] = dict(
                hits=sum(effective), total=len(effective)
            )
        if model_recalled:
            tracks[track]["model_candidate_recall_when_called"] = dict(
                hits=sum(model_recalled), total=len(model_recalled)
            )
    return dict(
        expected=len(cases),
        observed=len(predictions),
        tracks=tracks,
        details=details,
        human_reviewed=False,
        note=(
            "Synthetic sequence diagnostics; lexical state checks are not semantic "
            "correctness, downstream task success, or user satisfaction."
        ),
    )
