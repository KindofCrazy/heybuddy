"""Online Thread membership and scoped replay checkpoints; no duplicate message journal."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from personal_agent.core.history import HistoryError
from personal_agent.organization.models import Thread

SCHEMA = (
    """CREATE TABLE thread_scopes (
    thread_id TEXT NOT NULL REFERENCES topic_threads(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id), source TEXT NOT NULL,
    PRIMARY KEY(thread_id,conversation_id,source))""",
    """CREATE TABLE run_threads (
    run_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, source TEXT NOT NULL,
    thread_id TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, reason TEXT NOT NULL,
    FOREIGN KEY(run_id,conversation_id) REFERENCES runs(id,conversation_id),
    FOREIGN KEY(thread_id,conversation_id,source) REFERENCES thread_scopes)
    """,
    "CREATE INDEX run_thread_order ON run_threads(thread_id,conversation_id,source,run_id)",
    """CREATE TABLE active_threads (
    agent_id TEXT NOT NULL REFERENCES agents(id), conversation_id TEXT NOT NULL,
    source TEXT NOT NULL, thread_id TEXT NOT NULL,
    PRIMARY KEY(agent_id,conversation_id,source),
    FOREIGN KEY(thread_id,conversation_id,source) REFERENCES thread_scopes)""",
    """CREATE TABLE thread_route_events (
    id INTEGER PRIMARY KEY, run_id TEXT REFERENCES runs(id),
    thread_id TEXT NOT NULL REFERENCES topic_threads(id), previous_thread_id TEXT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id), source TEXT NOT NULL,
    reason TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE thread_context_summaries (
    thread_id TEXT NOT NULL, conversation_id TEXT NOT NULL, source TEXT NOT NULL,
    text TEXT NOT NULL CHECK(length(CAST(text AS BLOB))<=65536),
    through_sequence INTEGER NOT NULL CHECK(through_sequence>0), through_run_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision>0), updated_at TEXT NOT NULL, checksum TEXT NOT NULL,
    PRIMARY KEY(thread_id,conversation_id,source),
    FOREIGN KEY(thread_id,conversation_id,source) REFERENCES thread_scopes,
    FOREIGN KEY(through_run_id,conversation_id) REFERENCES runs(id,conversation_id))""",
)


def owners(db: sqlite3.Connection, run_ids: list[str]) -> dict[str, str]:
    # Lower-version migration/harness fixtures retain the original organization path.
    if (
        not run_ids
        or not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_threads'"
        ).fetchone()
    ):
        return {}
    return dict(
        db.execute(
            "SELECT run_id,thread_id FROM run_threads WHERE run_id IN ("
            + ",".join("?" for _ in run_ids)
            + ")",
            run_ids,
        ).fetchall()
    )


class RoutingRepository:
    def __init__(self, store, agent_id: str, conversation_id: str, source: str) -> None:
        self.store, self.agent_id = store, agent_id
        self.conversation_id, self.source = conversation_id, source

    def scope(self, db) -> None:
        if not db.execute(
            "SELECT 1 FROM conversations WHERE id=? AND agent_id=?",
            (self.conversation_id, self.agent_id),
        ).fetchone():
            raise HistoryError("Thread conversation is inaccessible.")

    def _thread(self, db, identifier: str) -> Thread:
        row = db.execute(
            "SELECT payload,revision,dirty,source_revision FROM topic_threads "
            "WHERE id=? AND agent_id=?",
            (identifier, self.agent_id),
        ).fetchone()
        if row is None:
            raise HistoryError("Thread is inaccessible.")
        try:
            value = Thread.model_validate_json(row["payload"])
            if value.id != identifier or value.revision != row["revision"]:
                raise ValueError
            return value.model_copy(
                update={"dirty": bool(row["dirty"]), "source_revision": row["source_revision"]}
            )
        except ValueError:
            raise HistoryError("Stored Thread is invalid.") from None

    def get(self, identifier: str) -> Thread:
        with self.store.reading() as db:
            self.scope(db)
            return self._thread(db, identifier)

    def _create(self, db, title: str) -> Thread:
        value = Thread(
            id=str(uuid4()),
            title=title.strip()[:120] or "新讨论",
            current_state=None,
            last_active_at=datetime.now(UTC),
        )
        db.execute(
            "INSERT INTO topic_threads(id,agent_id,revision,dirty,last_active_at,payload) "
            "VALUES(?,?,1,0,?,?)",
            (value.id, self.agent_id, value.last_active_at.isoformat(), value.model_dump_json()),
        )
        db.execute(
            "INSERT INTO thread_versions VALUES(?,1,?,'[]')", (value.id, value.model_dump_json())
        )
        return value

    def _select(self, db, identifier: str) -> None:
        self._thread(db, identifier)
        db.execute(
            "INSERT OR IGNORE INTO thread_scopes VALUES(?,?,?)",
            (identifier, self.conversation_id, self.source),
        )
        db.execute(
            "INSERT INTO active_threads VALUES(?,?,?,?) ON CONFLICT "
            "(agent_id,conversation_id,source) DO UPDATE SET thread_id=excluded.thread_id",
            (self.agent_id, self.conversation_id, self.source, identifier),
        )

    def create(self, title: str = "新讨论") -> Thread:
        with self.store._transaction() as db:
            self.scope(db)
            value = self._create(db, title)
            self._select(db, value.id)
            db.execute(
                "INSERT INTO thread_route_events(thread_id,conversation_id,source,reason) "
                "VALUES(?,?,?,'host_new')",
                (value.id, self.conversation_id, self.source),
            )
            return value

    def select(self, identifier: str) -> Thread:
        with self.store._transaction() as db:
            self.scope(db)
            self._select(db, identifier)
            db.execute(
                "INSERT INTO thread_route_events(thread_id,conversation_id,source,reason) "
                "VALUES(?,?,?,'host_select')",
                (identifier, self.conversation_id, self.source),
            )
            return self._thread(db, identifier)

    def active(self) -> Thread | None:
        with self.store.reading() as db:
            self.scope(db)
            row = db.execute(
                "SELECT thread_id FROM active_threads WHERE agent_id=? "
                "AND conversation_id=? AND source=?",
                (self.agent_id, self.conversation_id, self.source),
            ).fetchone()
            return self._thread(db, row[0]) if row else None

    def candidates(self) -> list[Thread]:
        with self.store.reading() as db:
            self.scope(db)
            rows = db.execute(
                "SELECT id FROM topic_threads WHERE agent_id=? "
                "ORDER BY last_active_at DESC,id LIMIT 24",
                (self.agent_id,),
            ).fetchall()
            return [self._thread(db, row[0]) for row in rows]

    def selection_pending(self) -> bool:
        with self.store.reading() as db:
            self.scope(db)
            row = db.execute(
                "SELECT reason FROM thread_route_events WHERE conversation_id=? AND source=? "
                "ORDER BY id DESC LIMIT 1",
                (self.conversation_id, self.source),
            ).fetchone()
            return bool(row and row[0] in {"host_new", "host_select"})

    def neighbours(self, run_id: str) -> list[dict]:
        with self.store.reading() as db:
            self.scope(db)
            rows = db.execute(
                "SELECT r.id,json_extract(m.payload,'$.role') AS role,"
                "substr(json_extract(m.payload,'$.text'),1,500) AS text "
                "FROM runs r JOIN messages m ON m.run_id=r.id "
                "WHERE r.conversation_id=? AND r.id<>? AND r.status IN ('completed','failed') "
                "AND json_extract(m.payload,'$.role') IN ('user','assistant') AND NOT EXISTS "
                "(SELECT 1 FROM run_threads rt WHERE rt.run_id=r.id AND rt.source<>?) "
                "ORDER BY m.sequence DESC LIMIT 4",
                (self.conversation_id, run_id, self.source),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]

    def bind(self, run_id: str, identifier: str, reason: str) -> Thread:
        with self.store._transaction() as db:
            self.scope(db)
            row = db.execute(
                "SELECT status FROM runs WHERE id=? AND conversation_id=?",
                (run_id, self.conversation_id),
            ).fetchone()
            if row is None or row[0] != "running":
                raise HistoryError("Routing requires the current running Run.")
            prior = db.execute(
                "SELECT thread_id FROM run_threads WHERE run_id=?", (run_id,)
            ).fetchone()
            if prior:
                if prior[0] != identifier:
                    raise HistoryError("Run is already routed.")
                return self._thread(db, identifier)
            selected = self._thread(db, identifier)
            if selected.title == "新讨论" and selected.current_state is None:
                text = db.execute(
                    "SELECT json_extract(payload,'$.text') FROM messages "
                    "WHERE run_id=? ORDER BY sequence LIMIT 1",
                    (run_id,),
                ).fetchone()[0]
                selected = selected.model_copy(
                    update={
                        "title": text.strip()[:120] or "图片讨论",
                        "revision": selected.revision + 1,
                    }
                )
                db.execute(
                    "UPDATE topic_threads SET revision=?,payload=? WHERE id=?",
                    (selected.revision, selected.model_dump_json(), identifier),
                )
                db.execute(
                    "INSERT INTO thread_versions VALUES(?,?,?,'[]')",
                    (identifier, selected.revision, selected.model_dump_json()),
                )
            active = db.execute(
                "SELECT thread_id FROM active_threads WHERE agent_id=? "
                "AND conversation_id=? AND source=?",
                (self.agent_id, self.conversation_id, self.source),
            ).fetchone()
            self._select(db, identifier)
            db.execute(
                "INSERT INTO run_threads(run_id,conversation_id,source,thread_id,reason) "
                "VALUES(?,?,?,?,?)",
                (run_id, self.conversation_id, self.source, identifier, reason),
            )
            db.execute(
                "INSERT INTO thread_route_events(run_id,thread_id,previous_thread_id,"
                "conversation_id,source,reason) VALUES(?,?,?,?,?,?)",
                (
                    run_id,
                    identifier,
                    active[0] if active else None,
                    self.conversation_id,
                    self.source,
                    reason,
                ),
            )
            return self._thread(db, identifier)

    def bootstrap(self) -> None:
        """Preserve unassigned legacy history, explicitly labelled as mixed historical dialogue."""
        with self.store._transaction() as db:
            self.scope(db)
            if db.execute(
                "SELECT 1 FROM active_threads WHERE agent_id=? AND conversation_id=? AND source=?",
                (self.agent_id, self.conversation_id, self.source),
            ).fetchone():
                return
            eligible = (
                "r.conversation_id=? AND r.status IN ('completed','failed') "
                "AND NOT EXISTS (SELECT 1 FROM run_threads rt WHERE rt.run_id=r.id) "
                "AND NOT EXISTS (SELECT 1 FROM harness_records h WHERE h.kind='run_scope' "
                "AND json_extract(h.payload,'$.run_id')=r.id AND h.source<>?)"
            )
            if not db.execute(
                "SELECT 1 FROM runs r WHERE " + eligible + " LIMIT 1",
                (self.conversation_id, self.source),
            ).fetchone():
                return
            value = self._create(db, "历史对话（未按主题整理）")
            self._select(db, value.id)
            db.execute(
                "INSERT OR IGNORE INTO thread_scopes "
                "SELECT DISTINCT e.thread_id,?,? FROM episode_runs er "
                "JOIN episodes e ON e.id=er.episode_id "
                "JOIN runs r ON r.id=er.run_id WHERE e.thread_id IS NOT NULL AND " + eligible,
                (self.conversation_id, self.source, self.conversation_id, self.source),
            )
            db.execute(
                "INSERT INTO run_threads(run_id,conversation_id,source,thread_id,reason) "
                "SELECT r.id,r.conversation_id,?,COALESCE((SELECT e.thread_id FROM episode_runs er "
                "JOIN episodes e ON e.id=er.episode_id WHERE er.run_id=r.id),?),'legacy' "
                "FROM runs r WHERE " + eligible,
                (self.source, value.id, self.conversation_id, self.source),
            )
            last = db.execute(
                "SELECT rt.thread_id FROM run_threads rt JOIN context_runs c ON c.run_id=rt.run_id "
                "WHERE rt.conversation_id=? AND rt.source=? ORDER BY c.last_sequence DESC LIMIT 1",
                (self.conversation_id, self.source),
            ).fetchone()
            if last:
                self._select(db, last[0])

    def correct(self, run_id: str, target: str) -> None:
        """Trusted correction: invalidate scoped summaries and detach any now-mixed Episode."""
        with self.store._transaction() as db:
            self.scope(db)
            row = db.execute(
                "SELECT rt.thread_id FROM run_threads rt JOIN runs r ON r.id=rt.run_id "
                "WHERE rt.run_id=? "
                "AND rt.conversation_id=? AND rt.source=? AND r.status<>'running'",
                (run_id, self.conversation_id, self.source),
            ).fetchone()
            if row is None:
                raise HistoryError("Run binding is inaccessible.")
            old = row[0]
            self._select(db, target)
            db.execute(
                "UPDATE run_threads SET thread_id=?,revision=revision+1,reason='host_correct' "
                "WHERE run_id=?",
                (target, run_id),
            )
            db.execute(
                "DELETE FROM thread_context_summaries WHERE conversation_id=? AND source=? "
                "AND thread_id IN (?,?)",
                (self.conversation_id, self.source, old, target),
            )
            episode = db.execute(
                "SELECT episode_id FROM episode_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if episode:
                members = [
                    r[0]
                    for r in db.execute(
                        "SELECT run_id FROM episode_runs WHERE episode_id=?", (episode[0],)
                    )
                ]
                linked = owners(db, members)
                homogeneous = len(linked) == len(members) and len(set(linked.values())) == 1
                db.execute(
                    "UPDATE episodes SET thread_id=?,thread_dirty=? WHERE id=?",
                    (target if homogeneous else None, int(homogeneous), episode[0]),
                )
            db.execute(
                "INSERT INTO thread_route_events(run_id,thread_id,previous_thread_id,"
                "conversation_id,source,reason) VALUES(?,?,?,?,?,'host_correct')",
                (run_id, target, old, self.conversation_id, self.source),
            )
