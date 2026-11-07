"""Three deliberately small, deterministic baselines; no training or gold access."""

import re
from datetime import datetime


def predict(track: str, body: dict) -> dict:
    if track == "thread_routing":
        text = body["current_message"].lower()
        terms = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]{2}", text))
        ranked = sorted(
            body["thread_candidates"],
            key=lambda t: (
                sum(
                    term in (t["title"] + " " + (t["current_state"] or "")).lower()
                    for term in terms
                ),
                t["last_active_at"],
            ),
            reverse=True,
        )
        target = ranked[0]["id"] if ranked else None
        return dict(
            action=("continue" if target == body["active_thread_id"] else "select")
            if target
            else "new",
            thread_id=target,
        )
    if track == "memory_learning":
        text = "\n".join(
            e["text"] for e in body["episode"]["events"] if e.get("speaker", "user") == "user"
        )
        path = "topics/inbox.md"
        return dict(
            action="create",
            target_path=path,
            changes=[dict(path=path, action="create", before=None, after="# Inbox\n" + text)],
        )
    candidate = body["candidates"][0]
    due = candidate.get("due_at")
    timely = (
        due
        and 0
        <= (datetime.fromisoformat(due) - datetime.fromisoformat(body["now"])).total_seconds()
        <= 86400
    )
    return dict(
        action="notify" if timely else "silent",
        proposal_ids=[candidate["id"]],
        next_review_at=None,
        messages_sent=0,
    )
