"""Agent-scoped one-shot schedule, durable due queue, attempts and outcome audit."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from personal_agent.core.evidence import SECRET
from personal_agent.scheduler.models import (
    ReminderError,
    ReminderGrant,
    ReminderRequest,
    SchedulerPolicy,
)
from personal_agent.storage.proactive import stamp

if TYPE_CHECKING:
    from personal_agent.storage.sqlite import SQLiteStore


SCHEMA = (
    """CREATE TABLE scheduled_reminders (
        id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id),
        conversation_id TEXT NOT NULL, request_id TEXT NOT NULL,
        text TEXT NOT NULL CHECK(length(CAST(text AS BLOB))<=4800),
        due_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN
          ('scheduled','blocked','sending','accepted','failed','uncertain','cancelled','expired')),
        revision INTEGER NOT NULL DEFAULT 1,
        mode TEXT NOT NULL DEFAULT 'unavailable' CHECK(mode IN ('unavailable','simulation')),
        recipient TEXT, grant_revision INTEGER, receipt_id TEXT,
        error_code TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(agent_id,request_id),
        FOREIGN KEY(conversation_id,agent_id) REFERENCES conversations(id,agent_id))""",
    "CREATE INDEX reminder_due ON scheduled_reminders(agent_id,status,due_at,id)",
    "CREATE INDEX reminder_expiry ON scheduled_reminders(agent_id,status,expires_at,id)",
    """CREATE TABLE reminder_attempts (
        reminder_id TEXT PRIMARY KEY REFERENCES scheduled_reminders(id),
        agent_id TEXT NOT NULL REFERENCES agents(id), recipient TEXT NOT NULL,
        grant_revision INTEGER NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
        status TEXT NOT NULL CHECK(status IN ('sending','accepted','failed','uncertain')),
        error_code TEXT NOT NULL DEFAULT '', receipt_id TEXT)""",
    """CREATE TABLE reminder_events (
        id INTEGER PRIMARY KEY, reminder_id TEXT NOT NULL REFERENCES scheduled_reminders(id),
        agent_id TEXT NOT NULL REFERENCES agents(id), created_at TEXT NOT NULL,
        action TEXT NOT NULL, revision INTEGER NOT NULL, code TEXT NOT NULL DEFAULT '')""",
    "CREATE INDEX reminder_event_order ON reminder_events(agent_id,reminder_id,id)",
)

CHAT_SCHEMA = (
    "ALTER TABLE scheduled_reminders ADD COLUMN source_scope TEXT NOT NULL DEFAULT 'local'",
    "CREATE INDEX reminder_chat_scope ON scheduled_reminders"
    "(agent_id,source_scope,conversation_id)",
)

REAL_SCHEMA = (
    "ALTER TABLE scheduled_reminders ADD COLUMN delivery_channel TEXT NOT NULL DEFAULT '' "
    "CHECK(delivery_channel IN ('','simulation','napcat'))",
    "CREATE INDEX reminder_delivery_due ON scheduled_reminders "
    "(agent_id,source_scope,conversation_id,status,due_at,id)",
)


def record(row) -> dict:
    value = dict(row)
    # Keep the schema-18 CHECK-constrained legacy field; expose the current channel as mode.
    value["mode"] = value["delivery_channel"] or value["mode"]
    return value


class ReminderRepository:
    def __init__(self, store: SQLiteStore, agent_id: str) -> None:
        self.store, self.agent_id = store, agent_id

    def _get(self, db, reminder_id: str) -> dict:
        row = db.execute(
            "SELECT * FROM scheduled_reminders WHERE id=? AND agent_id=?",
            (reminder_id, self.agent_id),
        ).fetchone()
        if not row:
            raise ReminderError("reminder_not_found")
        return record(row)

    def get(self, reminder_id: str) -> dict:
        with self.store.reading() as db:
            return self._get(db, reminder_id)

    def _event(self, db, item: dict, action: str, at: datetime, code: str = "") -> None:
        db.execute(
            "INSERT INTO reminder_events "
            "(reminder_id,agent_id,created_at,action,revision,code) VALUES (?,?,?,?,?,?)",
            (item["id"], self.agent_id, stamp(at), action, item["revision"], code),
        )

    def create(self, request: ReminderRequest, policy: SchedulerPolicy, at: datetime) -> dict:
        if SECRET.search(request.text):
            raise ReminderError("reminder_sensitive_content")
        with self.store._transaction() as db:
            existing = db.execute(
                "SELECT * FROM scheduled_reminders WHERE agent_id=? AND request_id=?",
                (self.agent_id, request.request_id),
            ).fetchone()
            if existing:
                if (
                    existing["conversation_id"] != request.conversation_id
                    or existing["source_scope"] != request.source_scope
                    or existing["due_at"] != stamp(request.due_at)
                    or existing["text"] != request.text
                ):
                    raise ReminderError("reminder_idempotency_conflict")
                return record(existing)
            if not db.execute(
                "SELECT 1 FROM conversations WHERE id=? AND agent_id=?",
                (request.conversation_id, self.agent_id),
            ).fetchone():
                raise ReminderError("reminder_scope_mismatch")
            if not at < request.due_at <= at + timedelta(seconds=policy.max_future_seconds):
                raise ReminderError("reminder_time_out_of_range")
            item_id = str(uuid4())
            db.execute(
                "INSERT INTO scheduled_reminders "
                "(id,agent_id,conversation_id,request_id,text,due_at,expires_at,status,"
                "created_at,updated_at,source_scope) VALUES (?,?,?,?,?,?,?,'scheduled',?,?,?)",
                (
                    item_id,
                    self.agent_id,
                    request.conversation_id,
                    request.request_id,
                    request.text,
                    stamp(request.due_at),
                    stamp(request.due_at + timedelta(seconds=policy.grace_seconds)),
                    stamp(at),
                    stamp(at),
                    request.source_scope,
                ),
            )
            item = self._get(db, item_id)
            self._event(db, item, "created", at, "reminder_recorded")
            return item

    def _transition(self, db, item: dict, status: str, code: str, at: datetime) -> dict:
        db.execute(
            "UPDATE scheduled_reminders SET status=?,error_code=?,revision=revision+1,"
            "updated_at=? WHERE id=? AND agent_id=?",
            (status, code, stamp(at), item["id"], self.agent_id),
        )
        changed = self._get(db, item["id"])
        self._event(db, changed, status, at, code)
        return changed

    def cancel(self, reminder_id: str, at: datetime) -> dict:
        with self.store._transaction() as db:
            item = self._get(db, reminder_id)
            if item["status"] not in {"scheduled", "blocked"}:
                return dict(
                    outcome="too_late" if item["status"] == "sending" else "terminal", reminder=item
                )
            if item["expires_at"] <= stamp(at):
                return dict(
                    outcome="expired",
                    reminder=self._transition(db, item, "expired", "delivery_window_elapsed", at),
                )
            return dict(
                outcome="cancelled", reminder=self._transition(db, item, "cancelled", "", at)
            )

    def advance(self, at: datetime, limit: int) -> int:
        if not 1 <= limit <= 64:
            raise ValueError("Invalid reminder batch size")
        with self.store._transaction() as db:
            rows = db.execute(
                "SELECT * FROM scheduled_reminders WHERE agent_id=? AND status='blocked' "
                "AND expires_at<=? ORDER BY expires_at,id LIMIT ?",
                (self.agent_id, stamp(at), limit),
            ).fetchall()
            if len(rows) < limit:
                rows.extend(
                    db.execute(
                        "SELECT * FROM scheduled_reminders WHERE agent_id=? AND status='scheduled' "
                        "AND due_at<=? ORDER BY due_at,id LIMIT ?",
                        (self.agent_id, stamp(at), limit - len(rows)),
                    ).fetchall()
                )
            for row in rows:
                expired = row["expires_at"] <= stamp(at)
                self._transition(
                    db,
                    dict(row),
                    "expired" if expired else "blocked",
                    "delivery_window_elapsed" if expired else "channel_unavailable",
                    at,
                )
            return len(rows)

    def next_delay(self, at: datetime, maximum: float) -> float:
        with self.store.reading() as db:
            due = db.execute(
                "SELECT MIN(due_at) FROM scheduled_reminders "
                "WHERE agent_id=? AND status='scheduled'",
                (self.agent_id,),
            ).fetchone()[0]
            expiry = db.execute(
                "SELECT MIN(expires_at) FROM scheduled_reminders "
                "WHERE agent_id=? AND status='blocked'",
                (self.agent_id,),
            ).fetchone()[0]
        next_at = min((value for value in (due, expiry) if value is not None), default=None)
        return (
            maximum
            if next_at is None
            else min(maximum, max(0.05, (datetime.fromisoformat(next_at) - at).total_seconds()))
        )

    def claim(self, item: dict, grant: ReminderGrant, at: datetime) -> bool:
        with self.store._transaction() as db:
            current = self._get(db, item["id"])
            if (
                current != item
                or current["status"] not in {"scheduled", "blocked"}
                or not grant.enabled
                or (
                    grant.channel == "napcat"
                    and (
                        grant.source_scope is None
                        or not grant.source_scope.startswith("chat:")
                        or grant.source_scope != current["source_scope"]
                    )
                )
                or grant.conversation_id != current["conversation_id"]
                or not current["due_at"] <= stamp(at) < current["expires_at"]
            ):
                return False
            changed = self._transition(db, current, "sending", "", at)
            db.execute(
                "UPDATE scheduled_reminders SET mode=?,delivery_channel=?,"
                "recipient=?,grant_revision=? "
                "WHERE id=? AND agent_id=?",
                (
                    "simulation" if grant.channel == "simulation" else "unavailable",
                    grant.channel,
                    grant.recipient,
                    grant.revision,
                    item["id"],
                    self.agent_id,
                ),
            )
            db.execute(
                "INSERT INTO reminder_attempts "
                "(reminder_id,agent_id,recipient,grant_revision,started_at,status) "
                "VALUES (?,?,?,?,?,'sending')",
                (changed["id"], self.agent_id, grant.recipient, grant.revision, stamp(at)),
            )
            return True

    def finish(
        self,
        reminder_id: str,
        status: str,
        at: datetime,
        *,
        code: str = "",
        receipt_id: str | None = None,
    ) -> None:
        if status not in {"accepted", "failed", "uncertain"}:
            raise ValueError("Invalid reminder outcome")
        with self.store._transaction() as db:
            item = self._get(db, reminder_id)
            if item["status"] != "sending":
                raise ReminderError("reminder_not_sending")
            self._transition(db, item, status, code, at)
            db.execute(
                "UPDATE scheduled_reminders SET receipt_id=? WHERE id=? AND agent_id=?",
                (receipt_id, reminder_id, self.agent_id),
            )
            db.execute(
                "UPDATE reminder_attempts SET status=?,error_code=?,receipt_id=?,ended_at=? "
                "WHERE reminder_id=? AND agent_id=?",
                (status, code, receipt_id, stamp(at), reminder_id, self.agent_id),
            )

    def recover(self, at: datetime, limit: int) -> int:
        # Only attempts possibly submitted to a Sender are uncertain; no auto replay.
        if not 1 <= limit <= 64:
            raise ValueError("Invalid reminder recovery size")
        with self.store._transaction() as db:
            rows = db.execute(
                "SELECT id FROM scheduled_reminders WHERE agent_id=? AND status='sending' "
                "ORDER BY due_at,id LIMIT ?",
                (self.agent_id, limit),
            ).fetchall()
            for row in rows:
                self._recover_attempt(db, row["id"], at)
            return len(rows)

    def _recover_attempt(self, db, reminder_id: str, at: datetime) -> None:
        item = self._get(db, reminder_id)
        self._transition(db, item, "uncertain", "send_interrupted", at)
        db.execute(
            "UPDATE reminder_attempts SET status='uncertain',error_code='send_interrupted',"
            "ended_at=? "
            "WHERE reminder_id=? AND agent_id=?",
            (stamp(at), reminder_id, self.agent_id),
        )

    def listing(
        self,
        limit: int = 12,
        cursor: int = 2**63 - 1,
        *,
        conversation_id: str | None = None,
        source_scope: str | None = None,
    ) -> dict:
        if not 1 <= limit <= 20 or not 1 <= cursor <= 2**63 - 1:
            raise ValueError("Invalid reminder page")
        filters = ["agent_id=?", "rowid<?"]
        parameters: list[str | int] = [self.agent_id, cursor]
        if conversation_id is not None:
            filters.append("conversation_id=?")
            parameters.append(conversation_id)
        if source_scope is not None:
            filters.append("source_scope=?")
            parameters.append(source_scope)
        with self.store.reading() as db:
            rows = db.execute(
                "SELECT rowid AS position,* FROM scheduled_reminders WHERE "
                + " AND ".join(filters)
                + " ORDER BY rowid DESC LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
        items = [record(row) for row in rows[:limit]]
        next_cursor = items[-1]["position"] if len(rows) > limit else None
        for item in items:
            item.pop("position")
            item["origin"] = "scheduled_reminder"
        return dict(items=items, next_cursor=next_cursor, has_more=next_cursor is not None)

    def delivery_due(
        self,
        at: datetime,
        grant: ReminderGrant,
        connected_at: datetime,
        limit: int,
    ) -> list[dict]:
        if not 1 <= limit <= 64:
            raise ValueError("Invalid reminder delivery page")
        with self.store.reading() as db:
            rows = db.execute(
                "SELECT * FROM scheduled_reminders WHERE agent_id=? AND source_scope=? "
                "AND conversation_id=? AND status IN ('scheduled','blocked') "
                "AND due_at>=? AND due_at<=? AND expires_at>? ORDER BY due_at,id LIMIT ?",
                (
                    self.agent_id,
                    grant.source_scope,
                    grant.conversation_id,
                    stamp(connected_at),
                    stamp(at),
                    stamp(at),
                    limit,
                ),
            ).fetchall()
            return [record(row) for row in rows]

    def events(self, reminder_id: str, after: int = 0, limit: int = 20) -> list[dict]:
        if not 0 <= after <= 2**63 - 1 or not 1 <= limit <= 20:
            raise ValueError("Invalid reminder event page")
        with self.store.reading() as db:
            self._get(db, reminder_id)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM reminder_events WHERE agent_id=? "
                    "AND reminder_id=? AND id>? ORDER BY id LIMIT ?",
                    (self.agent_id, reminder_id, after, limit),
                )
            ]
