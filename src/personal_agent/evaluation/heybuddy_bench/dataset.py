"""Input-only loading; gold is opened by the scorer after inference finishes."""

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

TRACKS = {
    "thread_routing": ["continue", "select", "new", "chat", "clarify"],
    "memory_learning": ["create", "update", "no_op", "reject", "review"],
    "proactive_decision": ["silent", "defer", "notify", "propose_task"],
}
FORBIDDEN = {
    "gold",
    "heybuddy_gold",
    "source_gold",
    "native_gold",
    "world_state",
    "expected_action",
    "acceptable_actions",
    "has_answer",
    "ground_truth",
    "function",
    "variant",
}
INPUT_FIELDS = {
    "thread_routing": {
        "now",
        "active_thread_id",
        "thread_candidates",
        "history",
        "recent_context",
        "current_message",
    },
    "memory_learning": {"now", "memory_files", "memory_catalogue", "episode"},
    "proactive_decision": {"now", "attention_context", "recent_user_evidence", "candidates"},
}


def read_rows(path: Path) -> list[dict]:
    rows = [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines() if s.strip()]
    ids = [r["case_id"] for r in rows]
    require(len(ids) == len(set(ids)), "Duplicate case IDs")
    return rows


def dump_rows(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows),
        encoding="utf-8",
        newline="\n",
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    require(result.tzinfo is not None, "Timezone required")
    return result


def clean(value) -> None:
    if isinstance(value, dict):
        require(not FORBIDDEN.intersection(value), "Answer-bearing input field")
        for child in value.values():
            clean(child)
    elif isinstance(value, list):
        for child in value:
            clean(child)


def validate_input(case: dict) -> None:
    track, body = case["track"], case["input"]
    require(track in TRACKS, "Unknown track")
    require(set(body) == INPUT_FIELDS[track], "Unexpected/missing input fields")
    clean(body)
    now = timestamp(body["now"])
    if track == "thread_routing":
        options = [t["id"] for t in body["thread_candidates"]]
        require(len(options) == len(set(options)), "Duplicate Thread IDs")
        require(
            body["active_thread_id"] is None or body["active_thread_id"] in options,
            "Unknown active Thread",
        )
        require(bool(body["current_message"]), "Empty message")
        for item in body["thread_candidates"]:
            require(timestamp(item["last_active_at"]) <= now, "Future Thread")
        for item in body["history"] + body["recent_context"]:
            require(timestamp(item["timestamp"]) <= now, "Future history")
            require(item["thread_id"] in options, "Unknown history Thread")
    else:
        events = (
            body["episode"]["events"]
            if track == "memory_learning"
            else body["recent_user_evidence"]
        )
        eids = [e["id"] for e in events]
        require(len(eids) == len(set(eids)), "Duplicate evidence")
        times = [timestamp(e["timestamp"]) for e in events]
        require(times == sorted(times) and all(t <= now for t in times), "Invalid evidence time")
        require(all(e["text"].strip() for e in events), "Empty evidence")
        if track == "memory_learning":
            from personal_agent.memory.store import MemoryFS

            fs = MemoryFS(Path("unused-validation-root"))
            require(
                all(e.get("speaker", "user") in {"user", "assistant"} for e in events),
                "Invalid evidence role",
            )
            for path in body["memory_files"]:
                fs.path(path)
        else:
            candidates = body["candidates"]
            require(1 <= len(candidates) <= 4, "Candidate count")
            require(len({c["id"] for c in candidates}) == len(candidates), "Duplicate proposal")
            for candidate in candidates:
                require(set(candidate["source_evidence_ids"]) <= set(eids), "Missing source")
                require(
                    candidate["status"] in {"open", "deferred", "resolved", "dismissed"},
                    "Invalid proposal status",
                )
                last = candidate.get("last_notified_at")
                require(last is None or timestamp(last) <= now, "Future receipt")


def load_inputs(directory: Path) -> list[dict]:
    rows = read_rows(directory / "inputs.jsonl")
    for row in rows:
        validate_input(row)
    return rows


def validate(directory: Path, *, expected: int = 50) -> dict:
    inputs = load_inputs(directory)
    gold_rows = read_rows(directory / "gold.jsonl")
    gold = {r["case_id"]: r for r in gold_rows}
    require({r["case_id"] for r in inputs} == set(gold), "Input/gold mismatch")
    require(Counter(r["track"] for r in inputs) == dict.fromkeys(TRACKS, expected), "Track count")
    for case in inputs:
        row = gold[case["case_id"]]
        body, track, g = case["input"], case["track"], row["gold"]
        require(row["track"] == track, "Track mismatch")
        require(g["action"] in TRACKS[track], "Invalid action")
        require(
            set(g.get("acceptable_actions", [g["action"]])) <= set(TRACKS[track]),
            "Invalid acceptable action",
        )
        require(
            g["action"] in g.get("acceptable_actions", [g["action"]]),
            "Canonical action must remain acceptable",
        )
        if track == "thread_routing":
            if g["action"] in {"continue", "select"}:
                require(
                    g["thread_id"] in {t["id"] for t in body["thread_candidates"]},
                    "Unknown gold Thread",
                )
                require(
                    (g["thread_id"] == body["active_thread_id"]) == (g["action"] == "continue"),
                    "Continue/select mismatch",
                )
        else:
            events = (
                body["episode"]["events"]
                if track == "memory_learning"
                else body["recent_user_evidence"]
            )
            require(set(g["evidence_ids"]) <= {e["id"] for e in events}, "Unknown gold evidence")
            if track == "memory_learning":
                files = body["memory_files"]
                if g["action"] == "create":
                    require(g["target_path"] not in files and bool(g["new_text"]), "Bad create")
                if g["action"] == "update":
                    old = g["old_text"]
                    require(
                        bool(old) and files.get(g["target_path"], "").count(old) == 1,
                        "Update span must be unique",
                    )
            else:
                require(
                    bool(g["proposal_ids"])
                    and set(g["proposal_ids"]) <= {p["id"] for p in body["candidates"]},
                    "Unknown gold proposal",
                )
                if g["action"] == "defer":
                    require(timestamp(g["next_review_at"]) > timestamp(body["now"]), "Bad defer")
        if case["source"]["type"] == "public_derived":
            source = row["provenance"]
            require(
                all(source.get(k) for k in ("source_id", "source_ref", "revision", "sha256")),
                "Missing provenance",
            )
            require("native_gold" in source and "adaptation" in source, "Missing native label")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if "total" in manifest:
        require(manifest["total"] == len(inputs), "Manifest total mismatch")
    for name, checksum in manifest["sha256"].items():
        require(sha256(directory / name) == checksum, "Manifest hash mismatch: " + name)
    return {
        "total": len(inputs),
        "counts": dict(Counter(r["track"] for r in inputs)),
        "human_reviewed": manifest["human_reviewed"],
    }
