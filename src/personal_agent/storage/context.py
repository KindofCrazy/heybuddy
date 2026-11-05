"""SQLite-only bounded Run pages and atomic conversation-summary checkpoints."""

import hashlib
import sqlite3
from datetime import UTC, datetime

from pydantic import TypeAdapter, ValidationError

from personal_agent.core.budget import ContextError
from personal_agent.core.history import (
    ConversationSummary,
    HistoryError,
    HistoryPage,
    HistoryTurn,
    summary_checksum,
    validate_turn,
)
from personal_agent.core.models import Message

MESSAGE = TypeAdapter(Message)
SCHEMA = (
    """CREATE TABLE context_runs (
        run_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
        first_sequence INTEGER NOT NULL, last_sequence INTEGER NOT NULL,
        message_count INTEGER NOT NULL, payload_bytes INTEGER NOT NULL,
        FOREIGN KEY(run_id,conversation_id) REFERENCES runs(id,conversation_id))""",
    "CREATE INDEX context_run_order ON context_runs(conversation_id,last_sequence)",
    "CREATE INDEX run_conversation_order ON runs(conversation_id,started_at,id)",
    "CREATE INDEX message_run_order ON messages(run_id,sequence)",
    """INSERT INTO context_runs
        SELECT r.id,r.conversation_id,MIN(m.sequence),MAX(m.sequence),COUNT(*),
        SUM(length(CAST(m.payload AS BLOB))) FROM runs r JOIN messages m ON m.run_id=r.id
        WHERE r.status IN ('completed','failed') GROUP BY r.id""",
    """CREATE TRIGGER context_run_finished AFTER UPDATE OF status ON runs
        WHEN NEW.status IN ('completed','failed') AND OLD.status='running' BEGIN
        INSERT INTO context_runs SELECT NEW.id,NEW.conversation_id,MIN(sequence),MAX(sequence),
        COUNT(*),SUM(length(CAST(payload AS BLOB))) FROM messages WHERE run_id=NEW.id; END""",
    """CREATE TABLE context_summaries (
        conversation_id TEXT PRIMARY KEY REFERENCES conversations(id),
        text TEXT NOT NULL CHECK(length(CAST(text AS BLOB))<=65536),
        through_sequence INTEGER NOT NULL CHECK(through_sequence>0),
        through_run_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
        updated_at TEXT NOT NULL, checksum TEXT NOT NULL,
        FOREIGN KEY(through_run_id,conversation_id) REFERENCES runs(id,conversation_id))""",
)


def summary(
    db: sqlite3.Connection,
    conversation_id: str,
    *,
    thread_id: str | None = None,
    source: str = "cli",
) -> ConversationSummary | None:
    if thread_id is None:
        row = db.execute(
            "SELECT * FROM context_summaries WHERE conversation_id=?", (conversation_id,)
        ).fetchone()
    else:
        row = db.execute(
            "SELECT text,through_sequence,through_run_id,revision,updated_at,checksum "
            "FROM thread_context_summaries WHERE thread_id=? AND conversation_id=? AND source=?",
            (thread_id, conversation_id, source),
        ).fetchone()
    if row is None:
        return None
    try:
        value = ConversationSummary.model_validate(
            {k: row[k] for k in row.keys() if k != "conversation_id"}
        )
        if (
            not value.text.strip()
            or len(value.text.encode()) > 65536
            or value.revision < 1
            or value.checksum
            != summary_checksum(
                value.text, value.through_sequence, value.through_run_id, value.revision
            )
        ):
            raise ValueError
        boundary = db.execute(
            "SELECT 1 FROM context_runs c JOIN runs r ON c.run_id=r.id "
            "WHERE c.conversation_id=? AND c.run_id=? AND c.last_sequence=? "
            "AND r.status IN ('completed','failed')",
            (conversation_id, value.through_run_id, value.through_sequence),
        ).fetchone()
        if boundary is None:
            raise ValueError
        if (
            thread_id is not None
            and not db.execute(
                "SELECT 1 FROM run_threads WHERE run_id=? AND thread_id=? "
                "AND source=? AND conversation_id=?",
                (value.through_run_id, thread_id, source, conversation_id),
            ).fetchone()
        ):
            raise ValueError
        return value
    except (ValidationError, ValueError, UnicodeError):
        raise HistoryError("Conversation summary or boundary is invalid.") from None


def page(
    db: sqlite3.Connection,
    conversation_id: str,
    after: int,
    *,
    through: int | None = None,
    max_runs: int = 32,
    max_bytes: int = 262144,
    completed_only: bool = False,
    thread_id: str | None = None,
    source: str = "cli",
) -> HistoryPage:
    if not 1 <= max_runs <= 64 or not 1 <= max_bytes <= 2_097_152 or after < 0:
        raise ValueError("Invalid history page budget")
    status_filter = (
        "r.status='completed'" if completed_only else "r.status IN ('completed','failed')"
    )
    scoped = (
        " AND EXISTS (SELECT 1 FROM run_threads rt WHERE rt.run_id=c.run_id "
        "AND rt.thread_id=? AND rt.source=?)"
        if thread_id is not None
        else ""
    )
    rows = db.execute(
        "SELECT c.* FROM context_runs c JOIN runs r ON c.run_id=r.id "
        "WHERE c.conversation_id=? AND c.last_sequence>? AND c.last_sequence<=? "
        f"AND {status_filter}" + scoped + " ORDER BY c.last_sequence LIMIT ?",
        (
            conversation_id,
            after,
            through if through is not None else 9223372036854775807,
            *((thread_id, source) if thread_id is not None else ()),
            max_runs + 1,
        ),
    ).fetchall()
    turns = []
    used = 0
    more = False
    for info in rows:
        if (
            len(turns) >= max_runs
            or used + info["payload_bytes"] > max_bytes
            or info["message_count"] > 2048
        ):
            if not turns:
                raise ContextError(
                    "history_run_too_large",
                    "One complete Run exceeds the bounded history page; adjust budget "
                    "or start a new conversation.",
                )
            more = True
            break
        # Check byte/count bounds before retrieving or deserializing any payload.
        actual = db.execute(
            "SELECT COUNT(*),COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM "
            "messages WHERE run_id=?",
            (info["run_id"],),
        ).fetchone()
        if actual[0] != info["message_count"] or actual[1] != info["payload_bytes"]:
            raise HistoryError("Context Run index does not match the message journal.")
        records = db.execute(
            "SELECT sequence,payload FROM messages WHERE run_id=? AND "
            "conversation_id=? ORDER BY sequence LIMIT 2049",
            (info["run_id"], conversation_id),
        ).fetchall()
        try:
            messages = [MESSAGE.validate_json(record["payload"]) for record in records]
            validate_turn(messages, complete=True)
            if (
                records[0]["sequence"] != info["first_sequence"]
                or records[-1]["sequence"] != info["last_sequence"]
            ):
                raise HistoryError("Context Run boundary does not match the journal.")
        except ValidationError:
            raise HistoryError("Stored message structure is invalid.") from None
        turns.append(
            HistoryTurn(
                run_id=info["run_id"],
                first_sequence=info["first_sequence"],
                last_sequence=info["last_sequence"],
                messages=messages,
                source_hash=hashlib.sha256(
                    "".join(r["payload"] for r in records).encode()
                ).hexdigest(),
            )
        )
        used += info["payload_bytes"]
    return HistoryPage(turns=turns, has_more=more)


def compact_through(
    db: sqlite3.Connection,
    conversation_id: str,
    recent_runs: int,
    *,
    thread_id: str | None = None,
    source: str = "cli",
) -> int:
    scoped = (
        " AND EXISTS (SELECT 1 FROM run_threads rt WHERE rt.run_id=c.run_id "
        "AND rt.thread_id=? AND rt.source=?)"
        if thread_id is not None
        else ""
    )
    row = db.execute(
        "SELECT c.last_sequence FROM context_runs c JOIN runs r ON c.run_id=r.id "
        "WHERE c.conversation_id=? AND r.status IN ('completed','failed') "
        + scoped
        + " ORDER BY c.last_sequence DESC LIMIT 1 OFFSET ?",
        (conversation_id, *((thread_id, source) if thread_id is not None else ()), recent_runs),
    ).fetchone()
    return row[0] if row else 0


def save_summary(
    db: sqlite3.Connection,
    conversation_id: str,
    text: str,
    previous: ConversationSummary | None,
    turns: list[HistoryTurn],
    *,
    thread_id: str | None = None,
    source: str = "cli",
) -> ConversationSummary:
    if not text.strip() or len(text.encode()) > 65536 or not turns:
        raise HistoryError("Invalid summary checkpoint.")
    current = summary(db, conversation_id, thread_id=thread_id, source=source)
    if current != previous:
        raise HistoryError("Summary checkpoint conflict.")
    after = previous.through_sequence if previous else 0
    through = turns[-1].last_sequence
    fresh = page(
        db,
        conversation_id,
        after,
        through=through,
        max_runs=len(turns),
        max_bytes=2_097_152,
        thread_id=thread_id,
        source=source,
    )
    if fresh.has_more or fresh.turns != turns or through <= after:
        raise HistoryError("Summary source changed or skipped replayable Runs.")
    revision = previous.revision + 1 if previous else 1
    value = ConversationSummary(
        text=text,
        through_sequence=through,
        through_run_id=turns[-1].run_id,
        revision=revision,
        updated_at=datetime.now(UTC).isoformat(),
        checksum=summary_checksum(text, through, turns[-1].run_id, revision),
    )
    if thread_id is not None:
        db.execute(
            "INSERT INTO thread_context_summaries VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(thread_id,conversation_id,source) DO UPDATE SET text=excluded.text,"
            "through_sequence=excluded.through_sequence,through_run_id=excluded.through_run_id,"
            "revision=excluded.revision,updated_at=excluded.updated_at,checksum=excluded.checksum",
            (
                thread_id,
                conversation_id,
                source,
                text,
                through,
                value.through_run_id,
                revision,
                value.updated_at,
                value.checksum,
            ),
        )
        return value
    db.execute(
        "INSERT INTO context_summaries VALUES (?,?,?,?,?,?,?) ON CONFLICT(conversation_id) "
        "DO UPDATE SET text=excluded.text,through_sequence=excluded.through_sequence,"
        "through_run_id=excluded.through_run_id,revision=excluded.revision,"
        "updated_at=excluded.updated_at,checksum=excluded.checksum",
        (
            conversation_id,
            text,
            through,
            value.through_run_id,
            revision,
            value.updated_at,
            value.checksum,
        ),
    )
    return value
