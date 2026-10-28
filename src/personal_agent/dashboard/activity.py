"""Recent persistent facts and bounded live observations; no event database."""

import base64
import json

from personal_agent.dashboard.repository import ObservatoryRepository
from personal_agent.runtime import ChatService


def activity(
    repository: ObservatoryRepository, service: ChatService, limit: int, cursor: str
) -> dict:
    events = []

    def add(kind, item, event_type, stamp, basis):
        if not stamp:
            return
        events.append(
            {
                "event_id": event_type + ":" + item["id"],
                "timestamp": stamp,
                "event_type": event_type,
                "status": item.get("status", item.get("state", "recorded")),
                "related_kind": kind,
                "related_object_id": item["id"],
                "origin": "persistent_fact",
                "timestamp_basis": basis,
            }
        )

    # Each source is a bounded recent metadata page, never complete history or bodies.
    for kind in ("runs", "episodes", "threads", "jobs"):
        for item in repository.listing(kind, 64)["items"]:
            if kind == "runs":
                add(
                    kind,
                    {**item, "status": "started"},
                    "run_started",
                    item["started_at"],
                    "started_at",
                )
                add(kind, item, "run_" + item["status"], item["ended_at"], "ended_at")
            elif kind == "episodes":
                add(kind, item, "episode_snapshot", item["ended_at"], "source_end_not_publish_time")
            elif kind == "threads":
                add(kind, item, "thread_snapshot", item["last_active_at"], "source_activity_time")
            else:
                add(
                    kind,
                    item,
                    ("curator_" if item["kind"] == "curator" else "memory_learning_")
                    + item["state"],
                    item["updated_at"],
                    "receipt_updated_at",
                )
    org = service.organization
    if org:
        events.extend(dict(event) for event in org.events)
        actor = org.character_state
        stored = actor.repository.current() if actor else None
        if stored:
            value = stored.model_dump(mode="json")
            add(
                "state",
                {"id": "current", "status": "updated"},
                "character_state_updated",
                value["updated_at"],
                "updated_at",
            )
            events[-1].update(
                {"run_id": stored.source_run_id, "character_state_id": str(stored.revision)}
            )
    events.sort(key=lambda event: (event["timestamp"], event["event_id"]), reverse=True)
    if cursor:
        try:
            timestamp, event_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if not isinstance(timestamp, str) or not isinstance(event_id, str):
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("Invalid activity cursor") from None
        events = [e for e in events if (e["timestamp"], e["event_id"]) < (timestamp, event_id)]
    more = len(events) > limit
    items = events[:limit]
    token = (
        base64.urlsafe_b64encode(
            json.dumps([items[-1]["timestamp"], items[-1]["event_id"]]).encode()
        ).decode()
        if more
        else None
    )
    return {
        "items": items,
        "next_cursor": token,
        "has_more": more,
        "scope_limited": True,
        "scope": "Recent 64 records per kind and 128 live stage observations; snapshots "
        "use source times where publication timestamps were not persisted.",
    }
