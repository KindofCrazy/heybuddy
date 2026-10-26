"""Bounded conversation reads, complete Run boundaries and summary checkpoints."""

import hashlib
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from personal_agent.core.budget import ContextError
from personal_agent.core.models import (
    AssistantMessage,
    Message,
    Model,
    RunResult,
    TextMessage,
    ToolMessage,
)


class HistoryError(Exception):
    """Sanitized persistence failure; no SQL or private content."""


class ConversationSummary(Model):
    text: str
    through_sequence: int
    through_run_id: str
    revision: int
    updated_at: str
    checksum: str


class HistoryTurn(Model):
    run_id: str
    first_sequence: int
    last_sequence: int
    messages: list[Message]
    source_hash: str


class HistoryPage(Model):
    turns: list[HistoryTurn]
    has_more: bool = False

    @property
    def messages(self) -> list[Message]:
        return [message for turn in self.turns for message in turn.messages]


def summary_checksum(text: str, through: int, run_id: str, revision: int) -> str:
    import json

    return hashlib.sha256(
        json.dumps([text, through, run_id, revision], ensure_ascii=False).encode()
    ).hexdigest()


def validate_turn(messages: list[Message], *, complete: bool = False) -> None:
    if not messages or not isinstance(messages[0], TextMessage) or messages[0].role != "user":
        raise HistoryError("Invalid run message sequence.")
    pending: dict[str, str] = {}
    seen = set()
    for message in messages[1:]:
        if isinstance(message, AssistantMessage):
            if pending:
                raise HistoryError("Tool results are missing before an assistant message.")
            for call in message.tool_calls:
                if call.id in seen:
                    raise HistoryError("Duplicate tool call ID in run.")
                seen.add(call.id)
                pending[call.id] = call.name
        elif isinstance(message, ToolMessage):
            if pending.pop(message.result.call_id, None) != message.result.name:
                raise HistoryError("Tool result does not match a pending call.")
        else:
            raise HistoryError("Unexpected user or system message inside a run.")
    if complete and (pending or not isinstance(messages[-1], AssistantMessage)):
        raise HistoryError("Cannot finish a run with incomplete messages.")


class ConversationHistory(Protocol):
    def summary(self) -> ConversationSummary | None: ...
    def page(
        self, after: int, *, through: int | None = None, max_runs: int = 32, max_bytes: int = 262144
    ) -> HistoryPage: ...
    def compact_through(self, recent_runs: int) -> int: ...
    def save_summary(
        self, text: str, previous: ConversationSummary | None, turns: list[HistoryTurn]
    ) -> ConversationSummary: ...
    def begin(self, user_message: TextMessage) -> str: ...
    def append(self, run_id: str, message: Message) -> None: ...
    def finish(self, run_id: str, result: RunResult) -> None: ...
    def interrupt(self, run_id: str, *, cancelled: bool = False) -> None: ...


class InMemoryHistory:
    """The same Run and checkpoint contract for small embedded/offline sessions."""

    def __init__(self) -> None:
        self.turns: list[HistoryTurn] = []
        self._summary: ConversationSummary | None = None
        self._pending: dict[str, list[Message]] = {}
        self._sequence = 0

    def summary(self) -> ConversationSummary | None:
        return self._summary.model_copy(deep=True) if self._summary else None

    def page(
        self, after: int, *, through: int | None = None, max_runs: int = 32, max_bytes: int = 262144
    ) -> HistoryPage:
        selected = []
        used = 0
        more = False
        for turn in self.turns:
            if turn.last_sequence <= after or (
                through is not None and turn.last_sequence > through
            ):
                continue
            size = sum(len(m.model_dump_json().encode()) for m in turn.messages)
            if len(selected) >= max_runs or used + size > max_bytes:
                if not selected:
                    raise ContextError(
                        "history_run_too_large", "One complete Run exceeds the history page budget."
                    )
                more = True
                break
            selected.append(turn.model_copy(deep=True))
            used += size
        return HistoryPage(turns=selected, has_more=more)

    def compact_through(self, recent_runs: int) -> int:
        return self.turns[-recent_runs - 1].last_sequence if len(self.turns) > recent_runs else 0

    def save_summary(
        self, text: str, previous: ConversationSummary | None, turns: list[HistoryTurn]
    ) -> ConversationSummary:
        if previous != self._summary or not turns:
            raise HistoryError("Summary checkpoint conflict.")
        boundary = turns[-1]
        after = previous.through_sequence if previous else 0
        fresh = self.page(
            after, through=boundary.last_sequence, max_runs=len(turns), max_bytes=2_097_152
        )
        if fresh.has_more or fresh.turns != turns or boundary.last_sequence <= after:
            raise HistoryError("Summary source changed or skipped replayable Runs.")
        revision = previous.revision + 1 if previous else 1
        self._summary = ConversationSummary(
            text=text,
            through_sequence=boundary.last_sequence,
            through_run_id=boundary.run_id,
            revision=revision,
            updated_at=datetime.now(UTC).isoformat(),
            checksum=summary_checksum(text, boundary.last_sequence, boundary.run_id, revision),
        )
        return self.summary()

    def begin(self, user_message: TextMessage) -> str:
        run_id = uuid4().hex
        self._pending[run_id] = [user_message.model_copy(deep=True)]
        return run_id

    def append(self, run_id: str, message: Message) -> None:
        validate_turn([*self._pending[run_id], message])
        self._pending[run_id].append(message.model_copy(deep=True))

    def finish(self, run_id: str, result: RunResult) -> None:
        messages = self._pending[run_id]
        validate_turn(messages, complete=True)
        raw = "".join(m.model_dump_json() for m in messages).encode()
        self.turns.append(
            HistoryTurn(
                run_id=run_id,
                first_sequence=self._sequence + 1,
                last_sequence=self._sequence + len(messages),
                messages=messages,
                source_hash=hashlib.sha256(raw).hexdigest(),
            )
        )
        self._sequence += len(messages)
        del self._pending[run_id]

    def interrupt(self, run_id: str, *, cancelled: bool = False) -> None:
        self._pending.pop(run_id, None)
