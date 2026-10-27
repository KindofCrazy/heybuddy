"""SQLite journal with short transactions; external tool effects are never transactional."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Self
from uuid import uuid4

from pydantic import BaseModel, TypeAdapter, ValidationError

from personal_agent.core.history import (
    ConversationSummary,
    HistoryError,
    HistoryPage,
    HistoryTurn,
    validate_turn,
)
from personal_agent.core.models import (
    Message,
    RunResult,
    TextMessage,
)
from personal_agent.storage import automation, context, episodes, recall, reflection, threads
from personal_agent.storage.lock import DatabaseLock
from personal_agent.storage.models import AgentRecord, ConversationRecord, RunRecord, StoredMessage

SCHEMA_VERSION = 7
MESSAGE = TypeAdapter(Message)
# Version 0 -> 1. Future changes append a migration instead of replacing existing tables.
MIGRATIONS = {
    1: (
        """CREATE TABLE agents (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, system_prompt TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
        """CREATE TABLE conversations (
        id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id), title TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(id, agent_id))""",
        """CREATE TABLE runs (
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
        status TEXT NOT NULL CHECK(status IN (
            'running','completed','failed','cancelled','interrupted')),
        started_at TEXT NOT NULL, ended_at TEXT, result_json TEXT,
        UNIQUE(id, conversation_id))""",
        """CREATE UNIQUE INDEX one_running_run ON runs(conversation_id) WHERE status='running'""",
        """CREATE TABLE messages (
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
        run_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK(sequence > 0),
        payload TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(conversation_id, sequence),
        FOREIGN KEY(run_id, conversation_id) REFERENCES runs(id, conversation_id))""",
        """CREATE TABLE client_state (
        agent_id TEXT PRIMARY KEY REFERENCES agents(id), conversation_id TEXT NOT NULL,
        FOREIGN KEY(conversation_id, agent_id) REFERENCES conversations(id, agent_id))""",
    ),
    2: recall.INDEX_SCHEMA,
    3: context.SCHEMA,
    4: reflection.SCHEMA,
    5: automation.SCHEMA,
    6: episodes.SCHEMA,
    7: threads.SCHEMA,
}


def now() -> str:
    return datetime.now(UTC).isoformat()


def decode_record[Record: BaseModel](kind: type[Record], row: sqlite3.Row) -> Record:
    try:
        return kind.model_validate(dict(row))
    except ValidationError:
        raise HistoryError("Stored record structure is invalid.") from None


class SQLiteStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._lock = DatabaseLock(Path(str(self.path) + ".lock"))
        try:
            self._connection = sqlite3.connect(self.path, timeout=2)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA synchronous=FULL")
        except sqlite3.Error:
            if hasattr(self, "_connection"):
                self._connection.close()
            self._lock.close()
            raise HistoryError("Cannot open conversation database.") from None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._connection.close()
        finally:
            self._lock.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            yield self._connection
            self._connection.commit()
        except BaseException as exc:
            try:
                self._connection.rollback()
            except sqlite3.Error:
                pass
            if isinstance(exc, sqlite3.Error):
                raise HistoryError("Conversation database operation failed.") from None
            raise

    def initialize(self) -> None:
        """Explicit, atomic version migration; refuse unknown or newer databases."""
        with self._transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise HistoryError("Database schema is newer than this application.")
            if (
                version == 0
                and db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchone()
            ):
                raise HistoryError("Refusing to initialize an unrecognized database.")
            for target in range(version + 1, SCHEMA_VERSION + 1):
                for statement in MIGRATIONS[target]:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version={target}")

    def create_agent(
        self, name: str, system_prompt: str, *, agent_id: str | None = None
    ) -> AgentRecord:
        record = AgentRecord(
            id=agent_id or str(uuid4()), name=name, system_prompt=system_prompt, created_at=now()
        )
        with self._transaction() as db:
            db.execute(
                "INSERT INTO agents VALUES (?,?,?,?)",
                (record.id, record.name, record.system_prompt, record.created_at.isoformat()),
            )
        return record

    def context_summary(self, conversation_id: str) -> ConversationSummary | None:
        with self._transaction() as db:
            return context.summary(db, conversation_id)

    def context_page(
        self,
        conversation_id: str,
        after: int,
        *,
        through: int | None = None,
        max_runs: int = 32,
        max_bytes: int = 262144,
    ) -> HistoryPage:
        with self._transaction() as db:
            return context.page(
                db, conversation_id, after, through=through, max_runs=max_runs, max_bytes=max_bytes
            )

    def compact_through(self, conversation_id: str, recent_runs: int) -> int:
        with self._transaction() as db:
            return context.compact_through(db, conversation_id, recent_runs)

    def save_context_summary(
        self,
        conversation_id: str,
        text: str,
        previous: ConversationSummary | None,
        turns: list[HistoryTurn],
    ) -> ConversationSummary:
        with self._transaction() as db:
            return context.save_summary(db, conversation_id, text, previous, turns)

    def rebuild_recall_index(self) -> None:
        """Explicitly replace only derived Recall objects, atomically, from messages."""
        with self._transaction() as db:
            recall.rebuild(db)

    def search_recall(self, agent_id: str, query: str, limit: int) -> list[sqlite3.Row]:
        with self._transaction() as db, recall.query_budget(db):
            return recall.search(db, agent_id, query, limit)

    def get_recall(
        self, agent_id: str, message_id: str, before: int, after: int
    ) -> list[sqlite3.Row]:
        with self._transaction() as db, recall.query_budget(db):
            return recall.get_window(db, agent_id, message_id, before, after)

    def get_agent(self, agent_id: str) -> AgentRecord | None:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        return decode_record(AgentRecord, row) if row else None

    def create_conversation(
        self,
        agent_id: str,
        title: str = "New conversation",
        *,
        activate: bool = False,
    ) -> ConversationRecord:
        stamp = now()
        record = ConversationRecord(
            id=str(uuid4()), agent_id=agent_id, title=title, created_at=stamp, updated_at=stamp
        )
        with self._transaction() as db:
            db.execute(
                "INSERT INTO conversations VALUES (?,?,?,?,?)",
                (record.id, agent_id, title, stamp, stamp),
            )
            if activate:
                db.execute(
                    "INSERT INTO client_state VALUES (?,?) ON CONFLICT(agent_id) "
                    "DO UPDATE SET conversation_id=excluded.conversation_id",
                    (agent_id, record.id),
                )
        return record

    def get_conversation(self, conversation_id: str) -> ConversationRecord:
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
        if row is None:
            raise HistoryError("Conversation not found.")
        return decode_record(ConversationRecord, row)

    def list_conversations(self, agent_id: str) -> list[ConversationRecord]:
        with self._transaction() as db:
            rows = db.execute(
                "SELECT * FROM conversations WHERE agent_id=? ORDER BY updated_at DESC, id",
                (agent_id,),
            ).fetchall()
        return [decode_record(ConversationRecord, row) for row in rows]

    def select_conversation(self, agent_id: str, conversation_id: str) -> None:
        with self._transaction() as db:
            db.execute(
                "INSERT INTO client_state VALUES (?,?) ON CONFLICT(agent_id) "
                "DO UPDATE SET conversation_id=excluded.conversation_id",
                (agent_id, conversation_id),
            )

    def last_conversation(self, agent_id: str) -> str | None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT conversation_id FROM client_state WHERE agent_id=?", (agent_id,)
            ).fetchone()
        return row[0] if row else None

    def _insert_message(
        self, db: sqlite3.Connection, conversation_id: str, run_id: str, message: Message
    ) -> None:
        sequence = db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM messages WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?)",
            (
                str(uuid4()),
                conversation_id,
                run_id,
                sequence,
                MESSAGE.dump_json(message).decode(),
                now(),
            ),
        )

    def begin_run(self, conversation_id: str, user_message: TextMessage) -> str:
        validate_turn([user_message])
        run_id = str(uuid4())
        with self._transaction() as db:
            db.execute(
                "INSERT INTO runs VALUES (?,?, 'running', ?,NULL,NULL)",
                (run_id, conversation_id, now()),
            )
            self._insert_message(db, conversation_id, run_id, user_message)
        return run_id

    @staticmethod
    def _run_messages(db: sqlite3.Connection, run_id: str) -> list[Message]:
        try:
            return [
                MESSAGE.validate_json(row[0])
                for row in db.execute(
                    "SELECT payload FROM messages WHERE run_id=? ORDER BY sequence", (run_id,)
                )
            ]
        except ValidationError:
            raise HistoryError("Stored message structure is invalid.") from None

    @staticmethod
    def _running(db: sqlite3.Connection, run_id: str) -> str:
        row = db.execute("SELECT conversation_id,status FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None or row[1] != "running":
            raise HistoryError("Run is not active.")
        return row[0]

    def append_message(self, run_id: str, message: Message) -> None:
        with self._transaction() as db:
            conversation_id = self._running(db, run_id)
            validate_turn([*self._run_messages(db, run_id), message])
            self._insert_message(db, conversation_id, run_id, message)

    def finish_run(self, run_id: str, result: RunResult) -> None:
        with self._transaction() as db:
            conversation_id = self._running(db, run_id)
            validate_turn(self._run_messages(db, run_id), complete=True)
            stamp = now()
            db.execute(
                "UPDATE runs SET status=?,ended_at=?,result_json=? WHERE id=?",
                (
                    "completed" if result.status == "completed" else "failed",
                    stamp,
                    result.model_dump_json(),
                    run_id,
                ),
            )
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (stamp, conversation_id))

    def interrupt_run(self, run_id: str, *, cancelled: bool = False) -> None:
        with self._transaction() as db:
            db.execute(
                "UPDATE runs SET status=?,ended_at=? WHERE id=? AND status='running'",
                ("cancelled" if cancelled else "interrupted", now(), run_id),
            )

    def recover_incomplete_runs(self) -> int:
        """Call once at startup under the database owner lock; never execute tools."""
        with self._transaction() as db:
            return db.execute(
                "UPDATE runs SET status='interrupted',ended_at=? WHERE status='running'", (now(),)
            ).rowcount

    def run_statistics(self, conversation_id: str) -> tuple[int, int, str]:
        with self._transaction() as db:
            counts = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(status IN "
                "('cancelled','interrupted')),0) FROM runs WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            latest = db.execute(
                "SELECT status FROM runs WHERE conversation_id=? ORDER BY started_at "
                "DESC,id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return counts[0], counts[1], latest[0] if latest else "none"

    def last_interaction(self, conversation_id: str) -> datetime | None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT started_at FROM runs WHERE conversation_id=? AND status!='running' "
                "ORDER BY started_at DESC,id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def list_runs(self, conversation_id: str) -> list[RunRecord]:
        with self._transaction() as db:
            rows = db.execute(
                "SELECT * FROM runs WHERE conversation_id=? ORDER BY started_at,id",
                (conversation_id,),
            ).fetchall()
        try:
            return [
                RunRecord(
                    id=row["id"],
                    conversation_id=row["conversation_id"],
                    status=row["status"],
                    started_at=row["started_at"],
                    ended_at=row["ended_at"],
                    result=RunResult.model_validate_json(row["result_json"])
                    if row["result_json"]
                    else None,
                )
                for row in rows
            ]
        except ValidationError:
            raise HistoryError("Stored run structure is invalid.") from None

    def load_messages(
        self, conversation_id: str, *, replayable_only: bool = False
    ) -> list[StoredMessage]:
        query = "SELECT m.* FROM messages m JOIN runs r ON m.run_id=r.id WHERE m.conversation_id=?"
        if replayable_only:
            query += " AND r.status IN ('completed','failed')"
        with self._transaction() as db:
            rows = db.execute(query + " ORDER BY m.sequence", (conversation_id,)).fetchall()
        try:
            records = [
                StoredMessage(
                    id=row["id"],
                    conversation_id=row["conversation_id"],
                    run_id=row["run_id"],
                    sequence=row["sequence"],
                    content=MESSAGE.validate_json(row["payload"]),
                    created_at=row["created_at"],
                )
                for row in rows
            ]
            if replayable_only:
                turns: dict[str, list[Message]] = {}
                for record in records:
                    turns.setdefault(record.run_id, []).append(record.content)
                for turn in turns.values():
                    validate_turn(turn, complete=True)
            return records
        except ValidationError:
            raise HistoryError("Stored message structure is invalid.") from None
