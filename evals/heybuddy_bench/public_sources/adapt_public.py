"""Explicit HeyBuddy annotations over frozen excerpts; no model-assisted relabelling."""

from datetime import datetime, timedelta


def iso(value):
    return datetime.fromisoformat(value).isoformat()


def adapt(record):
    source, excerpt = record["source"], record["excerpt"]
    if source in {"TIAGE", "IRC Disentanglement"}:
        return routing(record)
    if source in {"LongMemEval", "MemoryAgentBench"}:
        return memory(record)
    turns = excerpt["turns"]
    now = iso(turns[-1]["timestamp"] + "+08:00")
    events = [
        dict(id=f"E{i:03d}", timestamp=iso(t["timestamp"] + "+08:00"), text=t["text"])
        for i, t in enumerate(turns)
    ]
    body = dict(
        now=now,
        recent_user_evidence=events,
        attention_context=dict(availability="unknown", quiet_hours=["23:00", "08:00"]),
        candidates=[
            dict(
                id="P001",
                title="待跟进事项",
                topic_key="pending-item",
                status="open",
                context="根据原始用户交流核对当前跟进价值。",
                due_at=None,
                source_evidence_ids=[e["id"] for e in events],
                last_notified_at=None,
                followup_state="pending",
            )
        ],
    )
    positive = record["native_gold"]["variant"] == "positive_clean"
    quiet = datetime.fromisoformat(now).hour >= 23 or datetime.fromisoformat(now).hour < 8
    action = "notify" if positive and not quiet else "silent"
    reason = (
        "HeyBuddy default quiet-hours gate overrides native positive"
        if positive and quiet
        else "Positive opportunity"
        if positive
        else "Condition absent or follow-up already covered"
    )
    gold = dict(
        action=action,
        proposal_ids=["P001"],
        next_review_at=None,
        evidence_ids=[e["id"] for e in events],
        as_of=now,
    )
    return "proactive_decision", body, gold, reason


def routing(record):
    base = datetime.fromisoformat("2026-11-01T10:00:00+08:00")
    source, data = record["source"], record["excerpt"]
    if source == "TIAGE":
        prefix = data["prefix"]
        current = prefix[-1][0]
        history_text = [text for text, _ in prefix[:-1]]
        cid, turn = data["dialogue_id"], data["turn_index"]
        if cid == "1":
            title = "Evening conversation and going to a club" if turn == 3 else "Favorite season"
            action = "new" if turn == 3 else "continue"
        elif cid == "2":
            title = (
                "Visiting family and church" if turn == 4 else "Wheelchair and outdoor activities"
            )
            action = "new" if turn == 4 else "continue"
        else:
            title, action = "Work at a nursing home and advertising", "new"
        reason = "Topic labels are hints; annotation uses the visible dialogue and candidate scope."
    else:
        msg = data["message_index"]
        valid = [r["text"] for r in data["lines"] if not r["text"].startswith("===")]
        current, history_text = valid[-1], valid[:-1]
        # Participant handles are retained to disambiguate the reply, never executed as commands.
        title = "Firefox Java applet windows"
        action = "continue" if msg == 1002 else "new"
        reason = (
            "Adapted technical support turn; IRC participants remain quoted context, not users."
        )
    now = base + timedelta(minutes=len(history_text) + 1)
    body = dict(
        now=now.isoformat(),
        active_thread_id="T001",
        current_message=current,
        thread_candidates=[
            dict(
                id="T001",
                title=title,
                current_state="\n".join(history_text)[-1500:],
                last_active_at=(now - timedelta(minutes=1)).isoformat(),
            )
        ],
        history=[
            dict(
                episode_id="EP001",
                thread_id="T001",
                messages=history_text,
                timestamp=base.isoformat(),
            )
        ],
        recent_context=[
            dict(
                thread_id="T001", text=text, timestamp=(base + timedelta(minutes=i + 1)).isoformat()
            )
            for i, text in enumerate(history_text[-2:])
        ],
    )
    gold = dict(
        action=action,
        thread_id="T001" if action == "continue" else None,
        acceptable_actions=[action],
    )
    if source == "TIAGE" and data["dialogue_id"] == "2" and data["turn_index"] == 5:
        gold["acceptable_actions"] = ["continue", "new"]
        reason += " Hunting can reasonably be treated as a new topic."
    return "thread_routing", body, gold, reason


def memory(record):
    source, data = record["source"], record["excerpt"]
    if source == "LongMemEval":

        def parse(date):
            return datetime.strptime(
                date.split(" (")[0] + " " + date.split(") ")[-1], "%Y/%m/%d %H:%M"
            ).replace(tzinfo=datetime.fromisoformat("2026-01-01T00:00:00+08:00").tzinfo)

        messages = sorted(data["messages"], key=lambda m: (parse(m["date"]), m["message_index"]))
        old = messages[0]["text"]
        events = [
            dict(
                id=f"E{i:03d}",
                timestamp=(parse(m["date"]) + timedelta(seconds=i)).isoformat(),
                speaker="user",
                text=m["text"],
            )
            for i, m in enumerate(messages[1:])
        ]
        answer = str(record["native_gold"]["answer"])
        reason = (
            "Evidence-only knowledge update: prior user statement is preloaded; "
            "later source messages revise it."
        )
    else:
        facts = data["facts"]
        base = datetime.fromisoformat("2026-10-01T10:00:00+08:00")
        old = facts[0]["text"] if len(facts) > 1 else None
        new = facts[1:] if old else facts
        events = [
            dict(
                id=f"E{i:03d}",
                timestamp=(base + timedelta(minutes=i)).isoformat(),
                speaker="user",
                text="请整理我保存的合成世界设定笔记；按记录顺序保留最新设定：" + f["text"],
            )
            for i, f in enumerate(new)
        ]
        answer = str(record["native_gold"]["answers"][0])
        reason = (
            "Synthetic knowledge notes, not real user biography; "
            "exact relation revisions only, no QA evaluation."
        )
    path = "topics/reference-notes.md"
    files = {"human.md": "# Human\n"}
    if old:
        files[path] = "# Notes\n" + old
    now = events[-1]["timestamp"]
    body = dict(
        now=now,
        memory_files=files,
        memory_catalogue=list(files),
        episode=dict(id="EP001", thread_id=None, events=events),
    )
    gold = dict(
        action="update" if old else "create",
        target_path=path,
        old_text=files.get(path, ""),
        new_text="# Notes\n" + events[-1]["text"],
        facts=[],
        evidence_ids=[e["id"] for e in events],
        content_checks=[
            {
                "6a1eabeb": ["25:50", "25 minutes and 50", "25分50"],
                "6aeb4375": ["four", "四家", "4家"],
                "830ce83f": ["suburbs", "郊区"],
                "71315a70": ["10-12", "10–12", "10到12", "10 至 12"],
                "89941a93": ["four", "四辆", "4辆"],
                "ce6d2d27": ["Friday", "周五", "星期五"],
                "a1eacc2a": ["seven", "7", "七"],
                "184da446": ["220"],
            }.get(record["source_id"], [answer])
        ],
    )
    return "memory_learning", body, gold, reason
