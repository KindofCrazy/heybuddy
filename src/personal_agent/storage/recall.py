"""Derived FTS5 index and bounded, agent-scoped queries over canonical messages."""

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager

RECENT_LIMIT = 2000
SCAN_CHARS = 65536
SNIPPET_CHARS = 320
EXPAND_CHARS = 8192
ELIGIBLE = (
    "json_extract(payload,'$.role') IN ('user','assistant') "
    "AND typeof(json_extract(payload,'$.text'))='text' "
    "AND length(json_extract(payload,'$.text'))>0"
)

# External content is a VIEW over messages, never a second authoritative text store.
INDEX_SCHEMA = (
    f"CREATE VIEW recall_text AS SELECT rowid, json_extract(payload,'$.text') AS text "
    f"FROM messages WHERE {ELIGIBLE}",
    "CREATE VIRTUAL TABLE recall_fts USING fts5("
    "text, content='recall_text', content_rowid='rowid', tokenize='trigram')",
    "CREATE TRIGGER recall_messages_ai AFTER INSERT ON messages "
    f"WHEN {ELIGIBLE.replace('payload', 'new.payload')} BEGIN "
    "INSERT INTO recall_fts(rowid,text) VALUES(new.rowid,json_extract(new.payload,'$.text')); END",
    "CREATE TRIGGER recall_messages_ad AFTER DELETE ON messages "
    f"WHEN {ELIGIBLE.replace('payload', 'old.payload')} BEGIN "
    "INSERT INTO recall_fts(recall_fts,rowid,text) "
    "VALUES('delete',old.rowid,json_extract(old.payload,'$.text')); END",
    "CREATE TRIGGER recall_messages_au AFTER UPDATE ON messages BEGIN "
    "INSERT INTO recall_fts(recall_fts,rowid,text) "
    "SELECT 'delete',old.rowid,json_extract(old.payload,'$.text') "
    f"WHERE {ELIGIBLE.replace('payload', 'old.payload')}; "
    "INSERT INTO recall_fts(rowid,text) SELECT new.rowid,json_extract(new.payload,'$.text') "
    f"WHERE {ELIGIBLE.replace('payload', 'new.payload')}; END",
    "CREATE INDEX recall_conversations_agent ON conversations(agent_id,id)",
    "INSERT INTO recall_fts(recall_fts) VALUES('rebuild')",
)


def rebuild(db: sqlite3.Connection) -> None:
    for name in ("recall_messages_ai", "recall_messages_ad", "recall_messages_au"):
        db.execute(f"DROP TRIGGER IF EXISTS {name}")
    db.execute("DROP TABLE IF EXISTS recall_fts")
    db.execute("DROP VIEW IF EXISTS recall_text")
    db.execute("DROP INDEX IF EXISTS recall_conversations_agent")
    for statement in INDEX_SCHEMA:
        db.execute(statement)


@contextmanager
def query_budget(db: sqlite3.Connection) -> Iterator[None]:
    started = time.monotonic()
    steps = 0

    def expired() -> bool:
        nonlocal steps
        steps += 1000
        return steps > 5_000_000 or time.monotonic() - started > 0.5

    db.set_progress_handler(expired, 1000)
    try:
        yield
    finally:
        db.set_progress_handler(None, 0)


FIELDS = (
    "m.id AS message_id,m.conversation_id,m.run_id,m.sequence,m.created_at,"
    "r.status AS run_status,json_extract(m.payload,'$.role') AS role"
)
JOINS = (
    "FROM messages m JOIN runs r ON r.id=m.run_id AND r.conversation_id=m.conversation_id "
    "JOIN conversations c ON c.id=m.conversation_id "
)
VISIBLE = "c.agent_id=? AND r.status IN ('completed','failed')"
TEXT = "json_extract(m.payload,'$.text')"
TEXT_FILTER = ELIGIBLE.replace("payload", "m.payload")


def search(db: sqlite3.Connection, agent_id: str, query: str, limit: int) -> list[sqlite3.Row]:
    projection = (
        f"SELECT {FIELDS},substr({TEXT},max(1,instr(lower({TEXT}),lower(?))-64),?) AS text,"
        f"length(CAST({TEXT} AS BLOB)) AS original_bytes "
    )
    if len(query) >= 3:
        # The entire query is a literal FTS phrase; operators/quotes are not syntax.
        phrase = '"' + query.replace('"', '""') + '"'
        return db.execute(
            projection + JOINS + "JOIN recall_fts ON recall_fts.rowid=m.rowid "
            f"WHERE {VISIBLE} AND recall_fts MATCH ? "
            f"AND instr(lower({TEXT}),lower(?))>0 "
            "ORDER BY m.created_at DESC,m.id DESC LIMIT ?",
            (query, SNIPPET_CHARS, agent_id, phrase, query, limit + 1),
        ).fetchall()
    # MATERIALIZED ensures the cap applies BEFORE matching, not just to output hits.
    pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return db.execute(
        "WITH recent AS MATERIALIZED (SELECT m.rowid AS rid "
        + JOINS
        + f"WHERE {VISIBLE} AND {TEXT_FILTER} "
        "ORDER BY m.created_at DESC,m.id DESC LIMIT ?) "
        + projection
        + JOINS
        + "JOIN recent ON recent.rid=m.rowid "
        f"WHERE {VISIBLE} AND substr({TEXT},1,?) LIKE ? ESCAPE '\\' "
        "ORDER BY m.created_at DESC,m.id DESC LIMIT ?",
        (agent_id, RECENT_LIMIT, query, SNIPPET_CHARS, agent_id, SCAN_CHARS, pattern, limit + 1),
    ).fetchall()


def get_window(
    db: sqlite3.Connection, agent_id: str, message_id: str, before: int, after: int
) -> list[sqlite3.Row]:
    anchor = db.execute(
        f"SELECT {FIELDS} " + JOINS + f"WHERE {VISIBLE} AND m.id=? AND {TEXT_FILTER}",
        (agent_id, message_id),
    ).fetchone()
    if anchor is None:
        return []
    projection = (
        f"SELECT {FIELDS},substr({TEXT},1,?) AS text,"
        f"length(CAST({TEXT} AS BLOB)) AS original_bytes " + JOINS
    )
    params = (EXPAND_CHARS, agent_id, anchor["conversation_id"])
    earlier = db.execute(
        projection + f"WHERE {VISIBLE} AND m.conversation_id=? AND {TEXT_FILTER} "
        "AND m.sequence<? ORDER BY m.sequence DESC LIMIT ?",
        (*params, anchor["sequence"], before),
    ).fetchall()
    center = db.execute(
        projection + f"WHERE {VISIBLE} AND m.conversation_id=? AND m.id=?",
        (*params, message_id),
    ).fetchone()
    later = db.execute(
        projection + f"WHERE {VISIBLE} AND m.conversation_id=? AND {TEXT_FILTER} "
        "AND m.sequence>? ORDER BY m.sequence LIMIT ?",
        (*params, anchor["sequence"], after),
    ).fetchall()
    return [*reversed(earlier), center, *later]
