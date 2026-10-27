"""Small durable worthiness ledger; reflection remains the proposal/write authority."""

import json
import sqlite3
from typing import TYPE_CHECKING
from uuid import uuid4

from personal_agent.core.history import HistoryError, HistoryTurn
from personal_agent.reflection.models import ReflectionPlan
from personal_agent.storage import context
from personal_agent.storage.reflection import ReflectionRepository, sources

if TYPE_CHECKING:
    from personal_agent.storage.sqlite import SQLiteStore

SCHEMA = (
    """CREATE TABLE automation_batches (
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
        after_sequence INTEGER NOT NULL, through_sequence INTEGER NOT NULL,
        sources TEXT NOT NULL CHECK(length(CAST(sources AS BLOB))<=16384),
        state TEXT NOT NULL CHECK(state IN (
            'running','worthy','not_worthy','plan_created','failed','interrupted')),
        decision TEXT, plan_id TEXT REFERENCES reflection_plans(id),
        metrics TEXT NOT NULL, error_code TEXT NOT NULL DEFAULT '')""",
    """CREATE UNIQUE INDEX one_automation_batch ON automation_batches(conversation_id)
        WHERE state IN ('running','worthy','failed','interrupted')""",
    "CREATE INDEX automation_conversation ON automation_batches(conversation_id,through_sequence)",
)


class AutomationRepository:
    def __init__(self, store: "SQLiteStore", agent_id: str) -> None:
        self.store = store
        self.reflection = ReflectionRepository(store, agent_id)

    def active(self, conversation_id: str) -> dict | None:
        with self.store._transaction() as db:
            self.reflection._scope(db, conversation_id)
            row = db.execute(
                "SELECT * FROM automation_batches WHERE conversation_id=? "
                "AND state IN ('running','worthy','failed','interrupted') LIMIT 1",
                (conversation_id,),
            ).fetchone()
            return dict(row) if row else None

    def begin(self, conversation_id: str, after: int, turns: list[HistoryTurn]) -> dict:
        batch = dict(
            id=uuid4().hex,
            conversation_id=conversation_id,
            after_sequence=after,
            through_sequence=turns[-1].last_sequence,
            sources=json.dumps([source.model_dump() for source in sources(turns)]),
            state="running",
            decision=None,
            plan_id=None,
            metrics="{}",
            error_code="",
        )
        with self.store._transaction() as db:
            self.reflection._scope(db, conversation_id)
            self._verify(db, batch)
            db.execute(
                "INSERT INTO automation_batches VALUES (?,?,?,?,?,?,?,?,?,?)", tuple(batch.values())
            )
        return batch

    def _verify(self, db: sqlite3.Connection, batch: dict) -> None:
        row = db.execute(
            "SELECT through_sequence FROM reflection_progress WHERE conversation_id=?",
            (batch["conversation_id"],),
        ).fetchone()
        if (row[0] if row else 0) != batch["after_sequence"]:
            raise HistoryError("Automation source progress changed.")
        expected = json.loads(batch["sources"])
        fresh = context.page(
            db,
            batch["conversation_id"],
            batch["after_sequence"],
            through=batch["through_sequence"],
            max_runs=len(expected),
            max_bytes=2097152,
            completed_only=True,
        )
        if fresh.has_more or [source.model_dump() for source in sources(fresh.turns)] != expected:
            raise HistoryError("Automation source changed.")

    def update(
        self,
        batch: dict,
        *,
        state: str,
        metrics: dict,
        decision: dict | None = None,
        plan_id: str | None = None,
        error_code: str = "",
    ) -> None:
        with self.store._transaction() as db:
            self.reflection._scope(db, batch["conversation_id"])
            if decision is not None:
                self._verify(db, batch)
            if state == "not_worthy":
                if db.execute(
                    "SELECT 1 FROM reflection_plans WHERE conversation_id=? AND closed=0",
                    (batch["conversation_id"],),
                ).fetchone():
                    raise HistoryError("A pending plan prevents screening progress.")
                db.execute(
                    "INSERT INTO reflection_progress VALUES (?,?) ON CONFLICT(conversation_id) "
                    "DO UPDATE SET through_sequence=excluded.through_sequence",
                    (batch["conversation_id"], batch["through_sequence"]),
                )
            changed = db.execute(
                "UPDATE automation_batches SET state=?,metrics=?,"
                "decision=COALESCE(?,decision),plan_id=COALESCE(?,plan_id),error_code=? "
                "WHERE id=? AND conversation_id=? AND state IN "
                "('running','worthy','failed','interrupted','plan_created')",
                (
                    state,
                    json.dumps(metrics),
                    json.dumps(decision) if decision is not None else None,
                    plan_id,
                    error_code,
                    batch["id"],
                    batch["conversation_id"],
                ),
            ).rowcount
            if changed != 1:
                raise HistoryError("Automation batch changed.")

    def recover(self) -> None:
        with self.store._transaction() as db:
            db.execute(
                "UPDATE automation_batches SET state='interrupted',"
                "error_code='process_interrupted' "
                "WHERE state IN ('running','worthy') AND conversation_id IN "
                "(SELECT id FROM conversations WHERE agent_id=?)",
                (self.reflection.agent_id,),
            )

    def status(self, conversation_id: str) -> dict:
        progress = self.reflection.progress(conversation_id)
        plan = self.reflection.load(conversation_id)
        with self.store._transaction() as db:
            self.reflection._scope(db, conversation_id)
            boundary = plan.through_sequence if plan else progress
            backlog = db.execute(
                "SELECT COUNT(*) FROM context_runs c JOIN runs r ON r.id=c.run_id "
                "WHERE c.conversation_id=? AND r.status='completed' AND c.last_sequence>?",
                (conversation_id, boundary),
            ).fetchone()[0]
            stats = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(decision IS NOT NULL),0),"
                "COALESCE(SUM(json_extract(decision,'$.should_reflect')=1),0) "
                "FROM automation_batches WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            latest = db.execute(
                "SELECT state,error_code,metrics,plan_id FROM automation_batches "
                "WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return dict(
            reviewed_through=progress,
            pending_plan=plan.id if plan else None,
            unprocessed_runs=backlog,
            batches=stats[0],
            decisions=stats[1],
            triggers=stats[2],
            latest=({**dict(latest), "metrics": json.loads(latest["metrics"])} if latest else None),
        )

    def plan_for_batch(self, batch: dict) -> ReflectionPlan | None:
        with self.store._transaction() as db:
            self.reflection._scope(db, batch["conversation_id"])
            row = db.execute(
                "SELECT id FROM reflection_plans WHERE conversation_id=? "
                "AND json_extract(payload,'$.after_sequence')=? "
                "AND json_extract(payload,'$.through_sequence')=? ORDER BY rowid DESC LIMIT 1",
                (batch["conversation_id"], batch["after_sequence"], batch["through_sequence"]),
            ).fetchone()
        return self.reflection.load(batch["conversation_id"], row[0]) if row else None
