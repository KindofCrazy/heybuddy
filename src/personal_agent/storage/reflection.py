"""Agent-scoped reflection state; every transition is a short SQLite transaction."""

import hashlib
import sqlite3
from typing import TYPE_CHECKING

from pydantic import ValidationError

from personal_agent.core.history import HistoryError, HistoryPage, HistoryTurn
from personal_agent.reflection.models import TERMINAL, ReflectionPlan, SourceRun
from personal_agent.storage import context

if TYPE_CHECKING:
    from personal_agent.storage.sqlite import SQLiteStore

SCHEMA = (
    """CREATE TABLE reflection_progress (
        conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),
        through_sequence INTEGER NOT NULL CHECK(through_sequence>=0))""",
    """CREATE TABLE reflection_plans (
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
        closed INTEGER NOT NULL CHECK(closed IN (0,1)), revision INTEGER NOT NULL,
        payload TEXT NOT NULL CHECK(length(CAST(payload AS BLOB))<=262144),
        checksum TEXT NOT NULL)""",
    "CREATE UNIQUE INDEX one_reflection_plan ON reflection_plans(conversation_id) WHERE closed=0",
    "CREATE INDEX reflection_plan_conversation ON reflection_plans(conversation_id)",
)


def sources(turns: list[HistoryTurn]) -> list[SourceRun]:
    return [SourceRun(**turn.model_dump(exclude={"messages"})) for turn in turns]


class ReflectionRepository:
    def __init__(self, store: "SQLiteStore", agent_id: str) -> None:
        self.store = store
        self.agent_id = agent_id

    def _scope(self, db: sqlite3.Connection, conversation_id: str) -> None:
        if (
            db.execute(
                "SELECT 1 FROM conversations WHERE id=? AND agent_id=?",
                (conversation_id, self.agent_id),
            ).fetchone()
            is None
        ):
            raise HistoryError("Reflection conversation is inaccessible.")

    def progress(self, conversation_id: str) -> int:
        with self.store._transaction() as db:
            self._scope(db, conversation_id)
            row = db.execute(
                "SELECT through_sequence FROM reflection_progress WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            return row[0] if row else 0

    def window(
        self,
        conversation_id: str,
        after: int,
        *,
        max_runs: int,
        max_bytes: int,
        through: int | None = None,
    ) -> HistoryPage:
        with self.store._transaction() as db:
            self._scope(db, conversation_id)
            return context.page(
                db,
                conversation_id,
                after,
                through=through,
                max_runs=max_runs,
                max_bytes=max_bytes,
                completed_only=True,
            )

    def load(self, conversation_id: str, plan_id: str | None = None) -> ReflectionPlan | None:
        with self.store._transaction() as db:
            self._scope(db, conversation_id)
            row = db.execute(
                "SELECT * FROM reflection_plans WHERE conversation_id=? AND "
                + ("id=?" if plan_id else "closed=0")
                + " LIMIT 1",
                (conversation_id, plan_id) if plan_id else (conversation_id,),
            ).fetchone()
            if row is None:
                return None
            raw = row["payload"]
            try:
                value = ReflectionPlan.model_validate_json(raw)
                if (
                    hashlib.sha256(raw.encode()).hexdigest() != row["checksum"]
                    or value.id != row["id"]
                    or value.conversation_id != conversation_id
                    or value.revision != row["revision"]
                    or value.closed != bool(row["closed"])
                ):
                    raise ValueError
                return value
            except (ValidationError, ValueError):
                raise HistoryError("Reflection plan is invalid.") from None

    def save(self, plan: ReflectionPlan, *, expected_revision: int | None) -> ReflectionPlan:
        value = plan.model_copy(deep=True)
        value.closed = all(item.state in TERMINAL for item in value.items)
        value.revision = (expected_revision + 1) if expected_revision is not None else 1
        raw = value.model_dump_json()
        if len(raw.encode()) > 262144:
            raise HistoryError("Reflection plan exceeds its storage budget.")
        checksum = hashlib.sha256(raw.encode()).hexdigest()
        with self.store._transaction() as db:
            self._scope(db, value.conversation_id)
            if expected_revision is None:
                row = db.execute(
                    "SELECT through_sequence FROM reflection_progress WHERE conversation_id=?",
                    (value.conversation_id,),
                ).fetchone()
                if (row[0] if row else 0) != value.after_sequence:
                    raise HistoryError("Reflection progress changed.")
                fresh = context.page(
                    db,
                    value.conversation_id,
                    value.after_sequence,
                    through=value.through_sequence,
                    max_runs=len(value.sources),
                    max_bytes=2_097_152,
                    completed_only=True,
                )
                if fresh.has_more or sources(fresh.turns) != value.sources or not value.sources:
                    raise HistoryError("Reflection source changed or skipped completed Runs.")
                db.execute(
                    "INSERT INTO reflection_plans VALUES (?,?,?,?,?,?)",
                    (
                        value.id,
                        value.conversation_id,
                        int(value.closed),
                        value.revision,
                        raw,
                        checksum,
                    ),
                )
            else:
                changed = db.execute(
                    "UPDATE reflection_plans SET closed=?,revision=?,payload=?,checksum=? "
                    "WHERE id=? AND conversation_id=? AND revision=? AND closed=0",
                    (
                        int(value.closed),
                        value.revision,
                        raw,
                        checksum,
                        value.id,
                        value.conversation_id,
                        expected_revision,
                    ),
                ).rowcount
                if changed != 1:
                    raise HistoryError("Reflection plan changed or is already closed.")
            if value.closed:
                db.execute(
                    "INSERT INTO reflection_progress VALUES (?,?) "
                    "ON CONFLICT(conversation_id) DO UPDATE SET "
                    "through_sequence=excluded.through_sequence",
                    (value.conversation_id, value.through_sequence),
                )
        return value
