"""Bounded request audit; opaque provider data is deliberately not copied."""

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from personal_agent.core.history import HistoryError
from personal_agent.core.models import Message, ModelResponse, ToolDefinition

if TYPE_CHECKING:
    from personal_agent.storage.sqlite import SQLiteStore

MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
SCHEMA = (
    """CREATE TABLE model_rounds (
    run_id TEXT NOT NULL REFERENCES runs(id), number INTEGER NOT NULL CHECK(number>0),
    state TEXT NOT NULL CHECK(state IN
        ('requesting','responded','timeout','provider_error','runtime_error','cancelled')),
    started_at TEXT NOT NULL, ended_at TEXT, elapsed_ms INTEGER,
    message_count INTEGER NOT NULL, tool_count INTEGER NOT NULL,
    max_output_tokens INTEGER, input_tokens INTEGER, output_tokens INTEGER,
    reasoning_tokens INTEGER,
    prompt_state TEXT NOT NULL, prompt_json TEXT, response_state TEXT, response_json TEXT,
    PRIMARY KEY(run_id,number))""",
)


def snapshot(value: dict) -> tuple[str, str | None]:
    text = json.dumps(value, ensure_ascii=False, indent=2)
    return ("saved", text) if len(text.encode()) <= MAX_SNAPSHOT_BYTES else ("too_large", None)


class SQLiteRunTrace:
    def __init__(self, store: "SQLiteStore") -> None:
        self.store = store

    def start(
        self,
        run_id: str,
        number: int,
        messages: list[Message],
        tools: list[ToolDefinition],
        max_output_tokens: int | None,
    ) -> None:
        state, prompt = snapshot(
            {
                "messages": [m.model_dump(exclude={"provider_data"}) for m in messages],
                "tools": [t.model_dump() for t in tools],
                "max_output_tokens": max_output_tokens,
            }
        )
        with self.store._transaction() as db:
            row = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row or row["status"] != "running":
                raise HistoryError("Cannot record request for an inactive run.")
            db.execute(
                "INSERT INTO model_rounds (run_id,number,state,started_at,message_count,"
                "tool_count,max_output_tokens,prompt_state,prompt_json) "
                "VALUES (?,?,'requesting',?,?,?,?,?,?)",
                (
                    run_id,
                    number,
                    datetime.now(UTC).isoformat(),
                    len(messages),
                    len(tools),
                    max_output_tokens,
                    state,
                    prompt,
                ),
            )

    def finish(
        self,
        run_id: str,
        number: int,
        state: str,
        elapsed_ms: int,
        response: ModelResponse | None = None,
    ) -> None:
        response_state, body = (
            snapshot(response.model_dump(exclude={"provider_data"}))
            if response
            else ("unavailable", None)
        )
        usage = response.usage if response else None
        with self.store._transaction() as db:
            changed = db.execute(
                "UPDATE model_rounds SET state=?,ended_at=?,elapsed_ms=?,input_tokens=?,"
                "output_tokens=?,reasoning_tokens=?,response_state=?,response_json=? "
                "WHERE run_id=? AND number=? AND state='requesting'",
                (
                    state,
                    datetime.now(UTC).isoformat(),
                    elapsed_ms,
                    usage.input_tokens if usage else None,
                    usage.output_tokens if usage else None,
                    usage.reasoning_tokens if usage else None,
                    response_state,
                    body,
                    run_id,
                    number,
                ),
            ).rowcount
            if changed != 1:
                raise HistoryError("Request audit checkpoint is missing or already finished.")
