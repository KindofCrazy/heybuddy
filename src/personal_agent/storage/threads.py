"""Independent Thread snapshots, assignment provenance and optimistic publication."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import ValidationError

from personal_agent.core.history import HistoryError
from personal_agent.organization.models import Episode, Thread, ThreadDecision
from personal_agent.storage.episodes import EpisodeRepository

if TYPE_CHECKING:
    from personal_agent.storage.sqlite import SQLiteStore

SCHEMA = (
    """CREATE TABLE thread_organizer_state (
        agent_id TEXT PRIMARY KEY REFERENCES agents(id), revision INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'pending', error_code TEXT NOT NULL DEFAULT '',
        metrics TEXT NOT NULL DEFAULT '{}')""",
    """CREATE TABLE thread_versions (
        thread_id TEXT NOT NULL REFERENCES topic_threads(id), revision INTEGER NOT NULL,
        payload TEXT NOT NULL CHECK(length(CAST(payload AS BLOB))<=16384),
        episode_sources TEXT NOT NULL CHECK(length(CAST(episode_sources AS BLOB))<=8192),
        PRIMARY KEY(thread_id,revision))""",
    """CREATE TABLE thread_batches (
        agent_id TEXT NOT NULL REFERENCES agents(id), revision INTEGER NOT NULL,
        payload TEXT NOT NULL CHECK(length(CAST(payload AS BLOB))<=65536),
        PRIMARY KEY(agent_id,revision))""",
    "CREATE INDEX episode_pending_thread ON episodes(thread_dirty,ended_at)",
)


class ThreadRepository:
    def __init__(self, store: SQLiteStore, agent_id: str):
        self.store = store
        self.agent_id = agent_id
        self.episodes = EpisodeRepository(store, agent_id)

    def _thread(self, db: sqlite3.Connection, thread_id: str) -> Thread:
        row = db.execute(
            "SELECT * FROM topic_threads WHERE id=? AND agent_id=?", (thread_id, self.agent_id)
        ).fetchone()
        if row is None:
            raise HistoryError("Thread is inaccessible.")
        try:
            value = Thread.model_validate_json(row["payload"])
            if value.id != row["id"] or value.revision != row["revision"]:
                raise ValueError
            value.dirty = bool(row["dirty"])
            return value
        except (ValueError, ValidationError):
            raise HistoryError("Stored Thread is invalid.") from None

    def get(self, thread_id: str) -> Thread:
        with self.store._transaction() as db:
            return self._thread(db, thread_id)

    def _list(self, db: sqlite3.Connection, limit: int) -> list[Thread]:
        rows = db.execute(
            "SELECT id FROM topic_threads WHERE agent_id=? ORDER BY last_active_at DESC,id LIMIT ?",
            (self.agent_id, limit),
        ).fetchall()
        return [self._thread(db, row[0]) for row in rows]

    def list(self, limit: int = 24) -> list[Thread]:
        if not 1 <= limit <= 50:
            raise ValueError("Invalid Thread page size")
        with self.store._transaction() as db:
            return self._list(db, limit)

    def _pending(self, db: sqlite3.Connection, limit: int) -> list[Episode]:
        rows = db.execute(
            "SELECT e.id FROM episodes e JOIN conversations c ON c.id=e.stream_id "
            "WHERE c.agent_id=? AND e.thread_dirty=1 ORDER BY e.ended_at,e.id LIMIT ?",
            (self.agent_id, limit),
        ).fetchall()
        return [self.episodes._episode(db, row[0]) for row in rows]

    def _state(self, db: sqlite3.Connection) -> dict:
        row = db.execute(
            "SELECT * FROM thread_organizer_state WHERE agent_id=?", (self.agent_id,)
        ).fetchone()
        return (
            dict(row)
            if row
            else {
                "agent_id": self.agent_id,
                "revision": 0,
                "state": "pending",
                "error_code": "",
                "metrics": "{}",
            }
        )

    def status(self) -> dict:
        with self.store._transaction() as db:
            return {**self._state(db), "pending_episodes_capped": len(self._pending(db, 17))}

    def recover(self) -> None:
        with self.store._transaction() as db:
            db.execute(
                "UPDATE thread_organizer_state SET state='pending',error_code='' "
                "WHERE agent_id=? AND state='running'",
                (self.agent_id,),
            )

    def snapshot(self, limit: int) -> tuple[int, list[Episode], list[Thread]]:
        if not 1 <= limit <= 16:
            raise ValueError("Invalid Thread batch size")
        with self.store._transaction() as db:
            episodes = self._pending(db, limit)
            candidates = self._list(db, 24)
            # Include supplied episodes' existing owners even when outside recent 24.
            known = {t.id for t in candidates}
            for episode in episodes:
                if episode.thread_id and episode.thread_id not in known:
                    candidates.append(self._thread(db, episode.thread_id))
                    known.add(episode.thread_id)
            return self._state(db)["revision"], episodes, candidates

    def attempt(
        self, revision: int, state: str, code: str = "", metrics: dict | None = None
    ) -> None:
        with self.store._transaction() as db:
            if self._state(db)["revision"] != revision:
                raise HistoryError("Thread progress changed.")
            db.execute(
                "INSERT INTO thread_organizer_state VALUES (?,?,?,?,?) ON CONFLICT(agent_id) "
                "DO UPDATE SET state=excluded.state,error_code=excluded.error_code,"
                "metrics=excluded.metrics",
                (self.agent_id, revision, state, code, json.dumps(metrics or {})),
            )

    def publish(
        self,
        revision: int,
        episodes: list[Episode],
        candidates: list[Thread],
        decision: ThreadDecision,
        metrics: dict,
    ) -> None:
        ids = [eid for group in decision.groups for eid in group.episode_ids] + decision.no_thread
        if len(ids) != len(set(ids)) or set(ids) != {e.id for e in episodes}:
            raise HistoryError("Thread assignments must cover the provided Episodes exactly once.")
        known = {t.id: t for t in candidates}
        targets = [g.thread_id for g in decision.groups if g.thread_id]
        if len(targets) != len(set(targets)) or any(t not in known for t in targets):
            raise HistoryError("Thread target is not supplied or is duplicated.")
        with self.store._transaction() as db:
            if (
                self._state(db)["revision"] != revision
                or [self.episodes._episode(db, e.id) for e in episodes] != episodes
                or [self._thread(db, t.id) for t in candidates] != candidates
            ):
                raise HistoryError("Thread/Episode snapshot changed.")
            by_id = {e.id: e for e in episodes}
            new_targets = {}
            for group in decision.groups:
                thread_id = group.thread_id or str(uuid4())
                for eid in group.episode_ids:
                    new_targets[eid] = thread_id
            affected = {e.thread_id for e in episodes if e.thread_id}
            for old in affected:
                db.execute("UPDATE topic_threads SET dirty=1 WHERE id=?", (old,))
            for group in decision.groups:
                thread_id = new_targets[group.episode_ids[0]]
                previous = known.get(thread_id)
                last_active = max(by_id[eid].ended_at for eid in group.episode_ids)
                if previous:
                    last_active = max(last_active, previous.last_active_at)
                # Remain stale if there are omitted revisions still awaiting processing.
                pending_outside = db.execute(
                    "SELECT 1 FROM episodes WHERE thread_id=? AND thread_dirty=1 AND id NOT IN ("
                    + ",".join("?" for _ in episodes)
                    + ") LIMIT 1",
                    (thread_id, *(e.id for e in episodes)),
                ).fetchone()
                value = Thread(
                    id=thread_id,
                    title=group.title,
                    current_state=group.current_state,
                    last_active_at=last_active,
                    status=group.status,
                    memory_ref=previous.memory_ref if previous else None,
                    revision=previous.revision + 1 if previous else 1,
                    dirty=bool(pending_outside),
                )
                raw = value.model_dump_json()
                db.execute(
                    "INSERT INTO topic_threads VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "revision=excluded.revision,dirty=excluded.dirty,last_active_at=excluded.last_active_at,"
                    "payload=excluded.payload",
                    (
                        thread_id,
                        self.agent_id,
                        value.revision,
                        int(value.dirty),
                        value.last_active_at.isoformat(),
                        raw,
                    ),
                )
                sources = [
                    {"episode_id": eid, "revision": by_id[eid].revision}
                    for eid in group.episode_ids
                ]
                db.execute(
                    "INSERT INTO thread_versions VALUES (?,?,?,?)",
                    (thread_id, value.revision, raw, json.dumps(sources)),
                )
            for episode in episodes:
                db.execute(
                    "UPDATE episodes SET thread_id=?,thread_dirty=0 WHERE id=?",
                    (new_targets.get(episode.id), episode.id),
                )
            db.execute(
                "INSERT INTO thread_batches VALUES (?,?,?)",
                (
                    self.agent_id,
                    revision + 1,
                    json.dumps(
                        {
                            "episodes": [{"id": e.id, "revision": e.revision} for e in episodes],
                            "decision": decision.model_dump(),
                            "previous_threads": [
                                {"id": t.id, "revision": t.revision} for t in candidates
                            ],
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            db.execute(
                "UPDATE thread_organizer_state SET revision=?,state='completed',"
                "error_code='',metrics=? "
                "WHERE agent_id=?",
                (revision + 1, json.dumps(metrics), self.agent_id),
            )
