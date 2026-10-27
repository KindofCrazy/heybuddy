"""Agent-scoped bounded source snapshots and atomic Episode/progress publication."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError

from personal_agent.core.history import HistoryError
from personal_agent.organization.models import Batch, Episode, OrganizationPolicy, Source
from personal_agent.storage import context

if TYPE_CHECKING:
    from personal_agent.storage.sqlite import SQLiteStore


SCHEMA = (
    """CREATE TABLE topic_threads (
        id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id),
        revision INTEGER NOT NULL CHECK(revision>0), dirty INTEGER NOT NULL DEFAULT 0,
        last_active_at TEXT NOT NULL,
        payload TEXT NOT NULL CHECK(length(CAST(payload AS BLOB))<=16384),
        UNIQUE(id,agent_id))""",
    "CREATE INDEX thread_agent_order ON topic_threads(agent_id,last_active_at DESC)",
    """CREATE TABLE episodes (
        id TEXT PRIMARY KEY, stream_id TEXT NOT NULL REFERENCES conversations(id),
        revision INTEGER NOT NULL CHECK(revision>0),
        status TEXT NOT NULL CHECK(status IN ('provisional','finalized')),
        thread_id TEXT REFERENCES topic_threads(id), thread_dirty INTEGER NOT NULL DEFAULT 1,
        started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
        payload TEXT NOT NULL CHECK(length(CAST(payload AS BLOB))<=32768),
        UNIQUE(id,stream_id))""",
    "CREATE INDEX episode_stream_order ON episodes(stream_id,started_at DESC)",
    "CREATE INDEX episode_thread_order ON episodes(thread_id,started_at DESC)",
    """CREATE TRIGGER episode_thread_scope_insert BEFORE INSERT ON episodes
        WHEN NEW.thread_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM topic_threads t JOIN conversations c ON c.agent_id=t.agent_id
            WHERE t.id=NEW.thread_id AND c.id=NEW.stream_id)
        BEGIN SELECT RAISE(ABORT,'Thread scope mismatch'); END""",
    """CREATE TRIGGER episode_thread_scope_update BEFORE UPDATE OF thread_id,stream_id ON episodes
        WHEN NEW.thread_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM topic_threads t JOIN conversations c ON c.agent_id=t.agent_id
            WHERE t.id=NEW.thread_id AND c.id=NEW.stream_id)
        BEGIN SELECT RAISE(ABORT,'Thread scope mismatch'); END""",
    "CREATE UNIQUE INDEX one_provisional_episode ON episodes(stream_id) WHERE status='provisional'",
    """CREATE TABLE episode_runs (
        run_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL, stream_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal>=0), UNIQUE(episode_id,ordinal),
        FOREIGN KEY(run_id,stream_id) REFERENCES runs(id,conversation_id),
        FOREIGN KEY(episode_id,stream_id) REFERENCES episodes(id,stream_id))""",
    """CREATE TABLE episode_versions (
        episode_id TEXT NOT NULL REFERENCES episodes(id), revision INTEGER NOT NULL,
        payload TEXT NOT NULL CHECK(length(CAST(payload AS BLOB))<=32768),
        PRIMARY KEY(episode_id,revision))""",
    """CREATE TABLE episode_organizer_progress (
        stream_id TEXT PRIMARY KEY REFERENCES conversations(id),
        position INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'pending', error_code TEXT NOT NULL DEFAULT '',
        metrics TEXT NOT NULL DEFAULT '{}')""",
    """CREATE TABLE episode_skips (
        run_id TEXT PRIMARY KEY REFERENCES runs(id), stream_id TEXT NOT NULL REFERENCES
        conversations(id), reason TEXT NOT NULL)""",
    "CREATE INDEX organization_run_order ON runs(conversation_id)",
)


class EpisodeRepository:
    def __init__(self, store: SQLiteStore, agent_id: str) -> None:
        self.store = store
        self.agent_id = agent_id

    def scope(self, db: sqlite3.Connection, stream_id: str) -> None:
        if not db.execute(
            "SELECT 1 FROM conversations WHERE id=? AND agent_id=?", (stream_id, self.agent_id)
        ).fetchone():
            raise HistoryError("Organization stream is inaccessible.")

    def _progress(self, db: sqlite3.Connection, stream_id: str) -> dict:
        self.scope(db, stream_id)
        row = db.execute(
            "SELECT * FROM episode_organizer_progress WHERE stream_id=?", (stream_id,)
        ).fetchone()
        return (
            dict(row)
            if row
            else {
                "stream_id": stream_id,
                "position": 0,
                "revision": 0,
                "state": "pending",
                "error_code": "",
                "metrics": "{}",
            }
        )

    def status(self, stream_id: str) -> dict:
        with self.store._transaction() as db:
            progress = self._progress(db, stream_id)
            count = db.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM runs WHERE conversation_id=? "
                "AND rowid>? AND status!='running' LIMIT 65)",
                (stream_id, progress["position"]),
            ).fetchone()[0]
            latest = db.execute(
                "SELECT started_at FROM runs WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1",
                (stream_id,),
            ).fetchone()
            return {
                **progress,
                "pending_runs_capped": count,
                "last_started_at": latest[0] if latest else None,
            }

    def recover(self) -> None:
        # No publication occurs before the final transaction. Recomputing an interrupted
        # pure suggestion is safe; already committed source ranges cannot be duplicated.
        with self.store._transaction() as db:
            db.execute(
                "UPDATE episode_organizer_progress SET state='pending',error_code='' "
                "WHERE state='running' AND stream_id IN "
                "(SELECT id FROM conversations WHERE agent_id=?)",
                (self.agent_id,),
            )

    def attempt(
        self, batch: Batch, state: str, code: str = "", metrics: dict | None = None
    ) -> None:
        with self.store._transaction() as db:
            current = self._progress(db, batch.stream_id)
            if (current["position"], current["revision"]) != (batch.after, batch.revision):
                raise HistoryError("Organizer progress changed.")
            db.execute(
                "INSERT INTO episode_organizer_progress VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(stream_id) DO UPDATE SET state=excluded.state,"
                "error_code=excluded.error_code,metrics=excluded.metrics",
                (
                    batch.stream_id,
                    batch.after,
                    batch.revision,
                    state,
                    code,
                    json.dumps(metrics or {}),
                ),
            )

    def _episode(self, db: sqlite3.Connection, episode_id: str) -> Episode:
        row = db.execute(
            "SELECT e.* FROM episodes e JOIN conversations c ON c.id=e.stream_id "
            "WHERE e.id=? AND c.agent_id=?",
            (episode_id, self.agent_id),
        ).fetchone()
        if row is None:
            raise HistoryError("Episode is inaccessible.")
        try:
            value = Episode.model_validate_json(row["payload"])
            runs = db.execute(
                "SELECT run_id FROM episode_runs WHERE episode_id=? ORDER BY ordinal LIMIT 17",
                (episode_id,),
            ).fetchall()
            if (
                value.id != row["id"]
                or value.stream_id != row["stream_id"]
                or value.revision != row["revision"]
                or value.status != row["status"]
                or value.run_ids != [r[0] for r in runs]
            ):
                raise ValueError
            # Thread publication is independent of semantic Episode revisions.
            value.thread_id = row["thread_id"]
            value.thread_dirty = bool(row["thread_dirty"])
            return value
        except (ValidationError, ValueError):
            raise HistoryError("Stored Episode is invalid.") from None

    def get(self, episode_id: str) -> Episode:
        with self.store._transaction() as db:
            return self._episode(db, episode_id)

    def list(self, stream_id: str, limit: int = 20) -> list[Episode]:
        if not 1 <= limit <= 50:
            raise ValueError("Invalid Episode page size")
        with self.store._transaction() as db:
            self.scope(db, stream_id)
            rows = db.execute(
                "SELECT id FROM episodes WHERE stream_id=? ORDER BY started_at DESC,id LIMIT ?",
                (stream_id, limit),
            ).fetchall()
            return [self._episode(db, row[0]) for row in rows]

    def _source(self, db: sqlite3.Connection, row: sqlite3.Row, cap: int) -> Source:
        stats = db.execute(
            "SELECT COUNT(*),COALESCE(SUM(length(CAST(payload AS BLOB))),0),MAX(sequence) "
            "FROM messages WHERE run_id=?",
            (row["id"],),
        ).fetchone()
        reason = row["status"] if row["status"] != "completed" else None
        if not reason and (stats[0] > 2048 or stats[1] > cap):
            reason = "oversized"
        turn = None
        if not reason:
            page = context.page(
                db,
                row["conversation_id"],
                stats[2] - 1,
                through=stats[2],
                max_runs=1,
                max_bytes=cap,
                completed_only=True,
            )
            if len(page.turns) != 1 or page.turns[0].run_id != row["id"]:
                raise HistoryError("Completed source has no complete context index.")
            turn = page.turns[0]
            if turn.last_sequence - turn.first_sequence + 1 != len(turn.messages):
                raise HistoryError("Episode source sequence is not contiguous.")
        try:
            return Source(
                position=row["position"],
                run_id=row["id"],
                status=row["status"],
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                turn=turn,
                skip_reason=reason,
                payload_bytes=stats[1],
                message_count=stats[0],
            )
        except ValidationError:
            raise HistoryError("Invalid source time metadata.") from None

    def _batch(self, db: sqlite3.Connection, stream_id: str, policy: OrganizationPolicy) -> Batch:
        progress = self._progress(db, stream_id)
        latest = db.execute(
            "SELECT id FROM episodes WHERE stream_id=? AND status='provisional' LIMIT 1",
            (stream_id,),
        ).fetchone()
        previous = self._episode(db, latest[0]) if latest else None
        rows = db.execute(
            "SELECT rowid AS position,* FROM runs WHERE conversation_id=? AND rowid>? "
            "ORDER BY rowid LIMIT ?",
            (stream_id, progress["position"], policy.page_runs),
        ).fetchall()
        records = []
        used = 0
        for row in rows:
            if row["status"] == "running":
                break
            source = self._source(db, row, policy.source_bytes)
            if source.turn and used + source.payload_bytes > policy.source_bytes:
                break
            used += source.payload_bytes if source.turn else 0
            records.append(source)
        return Batch(
            stream_id=stream_id,
            after=progress["position"],
            revision=progress["revision"],
            records=records,
            previous=previous,
        )

    def batch(self, stream_id: str, policy: OrganizationPolicy) -> Batch:
        with self.store._transaction() as db:
            return self._batch(db, stream_id, policy)

    def sources(self, episode: Episode, policy: OrganizationPolicy) -> list[Source]:
        with self.store._transaction() as db:
            self.scope(db, episode.stream_id)
            result = []
            used = 0
            for run_id in episode.run_ids:
                row = db.execute(
                    "SELECT r.rowid AS position,r.* FROM runs r JOIN conversations c "
                    "ON c.id=r.conversation_id WHERE r.id=? AND c.agent_id=? "
                    "AND r.conversation_id=?",
                    (run_id, self.agent_id, episode.stream_id),
                ).fetchone()
                if row is None:
                    raise HistoryError("Episode source is inaccessible.")
                source = self._source(db, row, policy.source_bytes)
                used += source.payload_bytes
                if not source.turn or used > policy.source_bytes:
                    raise HistoryError("Episode source is unavailable or oversized.")
                result.append(source)
            return result

    def _write(self, db: sqlite3.Connection, episode: Episode, *, existing: bool = False) -> None:
        raw = episode.model_dump_json()
        values = (
            episode.stream_id,
            episode.revision,
            episode.status,
            episode.thread_id,
            int(episode.thread_dirty),
            episode.started_at.isoformat(),
            episode.ended_at.isoformat(),
            raw,
            episode.id,
        )
        if existing:
            db.execute(
                "UPDATE episodes SET stream_id=?,revision=?,status=?,thread_id=?,thread_dirty=?,"
                "started_at=?,ended_at=?,payload=? WHERE id=?",
                values,
            )
            db.execute("DELETE FROM episode_runs WHERE episode_id=?", (episode.id,))
        else:
            db.execute(
                "INSERT INTO episodes VALUES (?,?,?,?,?,?,?,?,?)", (episode.id, *values[:-1])
            )
        db.executemany(
            "INSERT INTO episode_runs VALUES (?,?,?,?)",
            [
                (run_id, episode.id, episode.stream_id, i)
                for i, run_id in enumerate(episode.run_ids)
            ],
        )
        db.execute(
            "INSERT INTO episode_versions VALUES (?,?,?)", (episode.id, episode.revision, raw)
        )
        if episode.thread_id and episode.thread_dirty:
            db.execute("UPDATE topic_threads SET dirty=1 WHERE id=?", (episode.thread_id,))

    def publish(
        self,
        batch: Batch,
        episodes: list[Episode],
        policy: OrganizationPolicy,
        previous_sources: list[Source],
        metrics: dict,
    ) -> None:
        with self.store._transaction() as db:
            fresh = self._batch(db, batch.stream_id, policy)
            if fresh != batch:
                raise HistoryError("Episode source or revision changed.")
            for source in previous_sources:
                row = db.execute(
                    "SELECT rowid AS position,* FROM runs WHERE id=?",
                    (source.run_id,),
                ).fetchone()
                if not row or self._source(db, row, policy.source_bytes) != source:
                    raise HistoryError("Provisional Episode source changed.")
            previous = batch.previous
            if previous and not any(e.id == previous.id for e in episodes):
                finalized = previous.model_copy(
                    update={
                        "status": "finalized",
                        "revision": previous.revision + 1,
                    }
                )
                self._write(db, finalized, existing=True)
            for episode in episodes:
                self._write(db, episode, existing=bool(previous and episode.id == previous.id))
            for record in batch.records:
                if record.skip_reason:
                    db.execute(
                        "INSERT INTO episode_skips VALUES (?,?,?)",
                        (record.run_id, batch.stream_id, record.skip_reason),
                    )
            position = batch.records[-1].position if batch.records else batch.after
            db.execute(
                "INSERT INTO episode_organizer_progress VALUES (?,?,?,'completed','',?) "
                "ON CONFLICT(stream_id) DO UPDATE SET position=excluded.position,"
                "revision=excluded.revision,state=excluded.state,error_code='',metrics=excluded.metrics",
                (batch.stream_id, position, batch.revision + 1, json.dumps(metrics)),
            )

    def finalize(self, stream_id: str, policy: OrganizationPolicy) -> bool:
        with self.store._transaction() as db:
            self.scope(db, stream_id)
            row = db.execute(
                "SELECT id FROM episodes WHERE stream_id=? AND status='provisional'",
                (stream_id,),
            ).fetchone()
            if not row:
                return False
            episode = self._episode(db, row[0])
            if (datetime.now(UTC) - episode.ended_at).total_seconds() < policy.revision_seconds:
                return False
            episode.status = "finalized"
            episode.revision += 1
            self._write(db, episode, existing=True)
            return True
