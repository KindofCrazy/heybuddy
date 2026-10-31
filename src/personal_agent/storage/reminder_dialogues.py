"""One short-lived clarification per trusted reminder scope and conversation."""

import json
from datetime import datetime, timedelta

from personal_agent.storage.proactive import stamp
from personal_agent.storage.reminders import ReminderRepository

SCHEMA = (
    """CREATE TABLE reminder_dialogues (
        agent_id TEXT NOT NULL REFERENCES agents(id), conversation_id TEXT NOT NULL,
        source_scope TEXT NOT NULL, payload TEXT NOT NULL
          CHECK(length(CAST(payload AS BLOB))<=8192), expires_at TEXT NOT NULL,
        PRIMARY KEY(agent_id,conversation_id,source_scope),
        FOREIGN KEY(conversation_id,agent_id) REFERENCES conversations(id,agent_id))""",
)


class ReminderDialogues:
    def __init__(self, repository: ReminderRepository) -> None:
        self.repository = repository

    def get(self, conversation: str, scope: str, now: datetime) -> dict | None:
        with self.repository.store.reading() as db:
            row = db.execute(
                "SELECT payload FROM reminder_dialogues WHERE agent_id=? AND conversation_id=? "
                "AND source_scope=? AND expires_at>?",
                (self.repository.agent_id, conversation, scope, stamp(now)),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def save(self, conversation: str, scope: str, payload: dict, now: datetime) -> None:
        raw = json.dumps(payload, ensure_ascii=False)
        with self.repository.store._transaction() as db:
            db.execute(
                "INSERT INTO reminder_dialogues VALUES (?,?,?,?,?) "
                "ON CONFLICT(agent_id,conversation_id,source_scope) DO UPDATE SET "
                "payload=excluded.payload,expires_at=excluded.expires_at",
                (
                    self.repository.agent_id,
                    conversation,
                    scope,
                    raw,
                    stamp(now + timedelta(minutes=15)),
                ),
            )

    def clear(self, conversation: str, scope: str) -> None:
        with self.repository.store._transaction() as db:
            db.execute(
                "DELETE FROM reminder_dialogues WHERE agent_id=? AND conversation_id=? "
                "AND source_scope=?",
                (self.repository.agent_id, conversation, scope),
            )
