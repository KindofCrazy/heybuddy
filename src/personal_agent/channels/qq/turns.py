"""Bounded receipt-prefix planning. Waiting never changes durable receipt state."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from personal_agent.channels.qq.config import QQSettings
from personal_agent.core.reminder_intent import reminder_action

MAX_TURN_MESSAGES = 16


@dataclass(frozen=True)
class QQTurn:
    messages: tuple[dict, ...]
    deadline: float

    def input_row(self) -> dict:
        # The newest message supplies the one passive reply reference/window.
        row = dict(self.messages[-1])
        attachments = []
        texts = []
        for message in self.messages:
            text = message["content"] or ""
            images = json.loads(message["attachments"])
            if images and len(self.messages) > 1:
                numbers = ", ".join(str(len(attachments) + i + 1) for i in range(len(images)))
                text += f"\n[本条消息附带图片：{numbers}]"
            texts.append(text)
            attachments.extend(images)
        row["content"] = "\n".join(texts)
        row["attachments"] = json.dumps(attachments)
        row["sequences"] = tuple(message["sequence"] for message in self.messages)
        row["receipts"] = self.messages
        return row


def plan_turn(
    rows: list[dict],
    settings: QQSettings,
    *,
    hold_after: frozenset[int] = frozenset(),
    extend_after: Mapping[int, float] | None = None,
) -> QQTurn:
    """Choose only a contiguous prefix, including historical idle/max-wait boundaries."""
    first = rows[0]
    selected = [first]
    first_at = datetime.fromisoformat(first["received_at"]).timestamp()
    extensions = extend_after or {}
    deadline = max(
        first_at + settings.qq_message_idle_seconds, extensions.get(first["sequence"], 0)
    )
    maximum = first_at + settings.qq_message_max_wait_seconds
    if first["sequence"] in hold_after:
        deadline = maximum
    command = (first["content"] or "").lstrip().startswith("/") or reminder_action(
        first["content"] or ""
    ) is not None
    if command:
        return QQTurn(tuple(selected), first_at)
    for row in rows[1:]:
        candidate = QQTurn(tuple([*selected, row]), deadline)
        combined = candidate.input_row()
        if (
            row["conversation_id"] != first["conversation_id"]
            or (row["content"] or "").lstrip().startswith("/")
            or reminder_action(row["content"] or "") is not None
            or datetime.fromisoformat(row["received_at"]).timestamp() >= min(deadline, maximum)
            or len(selected) >= MAX_TURN_MESSAGES
            or len(combined["content"].encode("utf-8")) > settings.qq_input_max_bytes
            or len(json.loads(combined["attachments"])) > settings.qq_image_max_count
        ):
            # A boundary already exists: no need to keep waiting for more messages.
            return QQTurn(tuple(selected), first_at)
        selected.append(row)
        deadline = max(
            datetime.fromisoformat(row["received_at"]).timestamp()
            + settings.qq_message_idle_seconds,
            extensions.get(row["sequence"], 0),
        )
        if row["sequence"] in hold_after:
            deadline = maximum
    if len(selected) >= MAX_TURN_MESSAGES:
        return QQTurn(tuple(selected), first_at)
    return QQTurn(tuple(selected), min(deadline, maximum))
