"""360-case suite inventory, frozen long-sequence validation and diagnostic scoring."""

import copy
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from personal_agent.evaluation.heybuddy_bench.sequence_data import (
    TRACKS,
    check_step,
    rows,
    sha,
)

DATA = Path("evals/heybuddy_bench/v1_0")


def validate(root=DATA):
    suite = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    inventory = []
    for subset in suite["subsets"]:
        path = root.parent / subset["path"]
        for name, checksum in subset["sha256"].items():
            if sha(path / name) != checksum:
                raise ValueError("Suite subset checksum mismatch")
        inputs = rows(path / "inputs.jsonl")
        if len(inputs) != subset["count"]:
            raise ValueError("Suite subset count mismatch")
        inventory.extend((subset["name"], c["case_id"]) for c in inputs)
    if len(inventory) != 360 or suite["total"] != 360 or len(set(inventory)) != 360:
        raise ValueError("Suite must contain 360 distinct named cases")
    cases, gold = rows(root / "long/inputs.jsonl"), rows(root / "long/gold.jsonl")
    manifest = json.loads((root / "long/manifest.json").read_text(encoding="utf-8"))
    if [c["case_id"] for c in cases] != [g["case_id"] for g in gold]:
        raise ValueError("Unaligned long inputs and Gold")
    families = defaultdict(set)
    messages, checks_count = 0, 0
    for case, answer in zip(cases, gold, strict=True):
        if set(case) != {"case_id", "track", "family", "split", "input"}:
            raise ValueError("Unknown case metadata")
        track, body = case["track"], case["input"]
        if track not in TRACKS or case["split"] not in {"dev", "holdout"}:
            raise ValueError("Unknown track/split")
        families[(track, case["family"])].add(case["split"])
        allowed = {
            "routing": {"threads", "active", "history", "steps"},
            "memory": {"memory_files", "steps"},
            "proactive": {"steps"},
        }[track]
        if set(body) != allowed:
            raise ValueError("Answer-bearing or unknown body field")
        steps = body["steps"]
        user_count = sum(bool(s.get("text")) for s in steps)
        if not 20 <= user_count <= 50 or len(steps) > 60:
            raise ValueError("Long scenario needs 20-50 new user messages")
        if len({s["id"] for s in steps}) != len(steps):
            raise ValueError("Duplicate step ID")
        for step in steps:
            if set(step) - {"id", "at", "text", "assistant", "learn", "attention"}:
                raise ValueError("Unknown step field")
        times = [datetime.fromisoformat(s["at"]) for s in steps]
        if any(t.tzinfo is None for t in times) or times != sorted(times):
            raise ValueError("Nonchronological or naive time")
        positions = {s["id"]: i for i, s in enumerate(steps)}
        seen_checks = set()
        for check in answer["checks"]:
            sid = check["step"]
            if (
                sid not in positions
                or sid in seen_checks
                or check["kind"] not in {"critical", "continuity", "final"}
            ):
                raise ValueError("Invalid checkpoint identity")
            seen_checks.add(sid)
            expected = check["expected"]
            target = expected.get("target")
            if (
                target
                and target.startswith("@")
                and (target[1:] not in positions or positions[target[1:]] >= positions[sid])
            ):
                raise ValueError("Future target reference")
            disclosed = {s["id"] for s in steps[: positions[sid] + 1] if s.get("text")}
            if any(set(o["evidence"]) - disclosed for o in expected.get("opportunities", [])):
                raise ValueError("Future evidence in checkpoint")
        if not answer["checks"] or answer["checks"][-1]["step"] != steps[-1]["id"]:
            raise ValueError("Every long scenario needs a final checkpoint")
        if track == "routing":
            for card in body["threads"]:
                if set(card) != {"id", "title", "current_state", "last_active_at"}:
                    raise ValueError("Unknown Thread field")
            for history in body["history"]:
                if (
                    set(history) != {"thread_id", "text", "timestamp"}
                    or datetime.fromisoformat(history["timestamp"]) >= times[0]
                ):
                    raise ValueError("Invalid historical input")
        messages += user_count
        checks_count += len(answer["checks"])
    if len(families) != 60 or any(len(s) != 1 for s in families.values()):
        raise ValueError("Family split leakage")
    counts = Counter(c["track"] for c in cases)
    if (
        counts != {t: 40 for t in TRACKS}
        or dict(Counter(c["split"] for c in cases)) != manifest["split"]
    ):
        raise ValueError("Long track/split counts differ")
    if messages != manifest["user_messages"] or checks_count != manifest["checkpoints"]:
        raise ValueError("Long message/checkpoint count differs")
    return dict(
        suite=360,
        long_scenarios=len(cases),
        families=len(families),
        user_messages=messages,
        checkpoints=checks_count,
        splits=manifest["split"],
        human_reviewed=False,
    )


def score(cases, gold, predictions):
    by_id = {p["case_id"]: p for p in predictions}
    if len(by_id) != len(predictions) or set(by_id) - {c["case_id"] for c in cases}:
        raise ValueError("Duplicate or unknown prediction")
    answers = {g["case_id"]: g["checks"] for g in gold}
    details, tracks = [], {}
    for case in cases:
        prediction = by_id.get(case["case_id"], {})
        observed = {s["step_id"]: s for s in prediction.get("steps", [])}
        if len(observed) != len(prediction.get("steps", [])):
            raise ValueError("Duplicate observed step")
        checks = []
        for check in answers[case["case_id"]]:
            expected = copy.deepcopy(check["expected"])
            if (target := expected.get("target")) and target.startswith("@"):
                expected["target"] = observed.get(target[1:], {}).get(
                    "thread_id", "__missing_target__"
                )
            actual = observed.get(check["step"])
            tests = check_step(case["track"], expected, actual)
            item = dict(
                step=check["step"],
                kind=check["kind"],
                passed=bool(tests) and all(tests.values()),
                assertions=tests,
            )
            if case["track"] == "routing" and expected.get("target"):
                item["candidate_recalled"] = bool(
                    actual and expected["target"] in actual.get("effective_candidates", [])
                )
            checks.append(item)
        execution_ok = not prediction.get("error_code") and not any(
            s.get("error_code") for s in observed.values()
        )
        complete = len(observed) == len(case["input"]["steps"])
        details.append(
            dict(
                case_id=case["case_id"],
                track=case["track"],
                family=case["family"],
                split=case["split"],
                user_messages=sum(bool(s.get("text")) for s in case["input"]["steps"]),
                observed_steps=len(observed),
                passed=complete and execution_ok and all(c["passed"] for c in checks),
                final_passed=checks[-1]["passed"],
                checks=checks,
                error_code=prediction.get("error_code"),
            )
        )
    for track in TRACKS:
        selected = [d for d in details if d["track"] == track]
        checkpoints = [c for d in selected for c in d["checks"]]
        critical = [c for c in checkpoints if c["kind"] != "continuity"]
        recalled = [c["candidate_recalled"] for c in checkpoints if "candidate_recalled" in c]
        tracks[track] = dict(
            scenarios=len(selected),
            passed=sum(d["passed"] for d in selected),
            final_passed=sum(d["final_passed"] for d in selected),
            checkpoints=len(checkpoints),
            checkpoints_passed=sum(c["passed"] for c in checkpoints),
            critical_checkpoints=len(critical),
            critical_passed=sum(c["passed"] for c in critical),
            user_messages=sum(d["user_messages"] for d in selected),
            effective_candidate_recall=dict(hits=sum(recalled), total=len(recalled)),
        )
    return dict(
        expected=len(cases),
        observed=len(predictions),
        tracks=tracks,
        details=details,
        human_reviewed=False,
    )
