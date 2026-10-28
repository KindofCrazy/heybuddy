"""Minimal QQ receipt ledger, not a second conversation history or job platform."""

from __future__ import annotations

from typing import TYPE_CHECKING

from personal_agent.channels.qq.config import QQError

if TYPE_CHECKING:
    from personal_agent.channels.qq.events import QQText
    from personal_agent.storage.sqlite import SQLiteStore

SCHEMA = (
    """CREATE TABLE qq_receipts (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL REFERENCES agents(id), app_id TEXT NOT NULL,
        openid TEXT NOT NULL, message_id TEXT NOT NULL, event_id TEXT NOT NULL,
        message_index TEXT NOT NULL, conversation_id TEXT NOT NULL,
        platform_at TEXT NOT NULL, received_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN
            ('received','processing','processed','sending','replied','failed','uncertain')),
        content TEXT, run_id TEXT REFERENCES runs(id),
        sent_parts INTEGER NOT NULL DEFAULT 0, error_code TEXT,
        UNIQUE(agent_id, app_id, message_id),
        FOREIGN KEY(conversation_id, agent_id) REFERENCES conversations(id, agent_id))""",
    """CREATE UNIQUE INDEX qq_event_id ON qq_receipts(agent_id, app_id, event_id)
        WHERE event_id != ''""",
    """CREATE INDEX qq_pending ON qq_receipts(agent_id, app_id, openid, status, sequence)""",
)


class QQInbox:
    def __init__(self, store: SQLiteStore, agent_id: str, app_id: str, openid: str) -> None:
        self.store = store
        self.agent_id = agent_id
        self.app_id = app_id
        self.openid = openid
        self.scope = (agent_id, app_id, openid)

    def recover(self) -> None:
        # A crash after claiming might have run tools or sent a reply: never replay it.
        with self.store._transaction() as db:
            db.execute(
                """UPDATE qq_receipts SET status='uncertain', content=NULL,
                    error_code='qq_process_interrupted' WHERE agent_id=? AND app_id=?
                    AND status IN ('processing','processed','sending')""",
                (self.agent_id, self.app_id),
            )

    def receive(self, message: QQText, conversation_id: str, capacity: int) -> bool:
        if message.openid != self.openid:
            raise QQError("qq_scope_mismatch", fatal=True)
        with self.store._transaction() as db:
            existing = db.execute(
                """SELECT 1 FROM qq_receipts WHERE agent_id=? AND app_id=?
                    AND (message_id=? OR (event_id != '' AND event_id=?)) LIMIT 1""",
                (self.agent_id, self.app_id, message.message_id, message.event_id),
            ).fetchone()
            if existing:
                return False
            count = db.execute(
                """SELECT count(*) FROM qq_receipts WHERE agent_id=? AND app_id=? AND openid=?
                    AND status='received'""",
                self.scope,
            ).fetchone()[0]
            if count >= capacity:
                raise QQError("qq_queue_full")
            db.execute(
                """INSERT INTO qq_receipts (agent_id, app_id, openid, message_id, event_id,
                    message_index, conversation_id, platform_at, received_at, status, content)
                    VALUES (?,?,?,?,?,?,?,?,?,'received',?)""",
                (
                    *self.scope,
                    message.message_id,
                    message.event_id,
                    message.message_index,
                    conversation_id,
                    message.platform_at.isoformat(),
                    message.received_at.isoformat(),
                    message.text,
                ),
            )
            return True

    def claim(self, conversation_id: str) -> dict | None:
        with self.store._transaction() as db:
            # Local CLI can change selection between launches; never reroute queued text.
            db.execute(
                """UPDATE qq_receipts SET status='failed', content=NULL,
                    error_code='qq_conversation_changed' WHERE agent_id=? AND app_id=?
                    AND openid=? AND status='received' AND conversation_id != ?""",
                (*self.scope, conversation_id),
            )
            row = db.execute(
                """SELECT * FROM qq_receipts WHERE agent_id=? AND app_id=? AND openid=?
                    AND status='received' ORDER BY sequence LIMIT 1""",
                self.scope,
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE qq_receipts SET status='processing', content=NULL WHERE sequence=?",
                (row["sequence"],),
            )
            return dict(row)

    def mark(
        self,
        sequence: int,
        status: str,
        *,
        sent_parts: int = 0,
        error_code: str | None = None,
        run_id: str | None = None,
    ) -> None:
        with self.store._transaction() as db:
            db.execute(
                """UPDATE qq_receipts SET status=?, content=NULL, sent_parts=?, error_code=?,
                    run_id=coalesce(?,run_id) WHERE sequence=? AND agent_id=? AND app_id=?
                    AND openid=?""",
                (status, sent_parts, error_code, run_id, sequence, *self.scope),
            )

    def latest_run_id(self, conversation_id: str) -> str | None:
        with self.store.reading() as db:
            row = db.execute(
                "SELECT m.run_id FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                "WHERE m.conversation_id=? AND c.agent_id=? ORDER BY m.sequence DESC LIMIT 1",
                (conversation_id, self.agent_id),
            ).fetchone()
        return row[0] if row else None

    def pending(self) -> bool:
        with self.store._transaction() as db:
            return (
                db.execute(
                    """SELECT 1 FROM qq_receipts WHERE agent_id=? AND app_id=? AND openid=?
                    AND status='received' LIMIT 1""",
                    self.scope,
                ).fetchone()
                is not None
            )
