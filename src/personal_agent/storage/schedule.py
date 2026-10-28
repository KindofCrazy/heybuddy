"""One Agent-owned schedule row; short metadata-only transactions, no model requests."""

import json
from datetime import UTC, datetime, timedelta

from personal_agent.storage.episodes import EpisodeRepository


class ScheduleRepository:
    def __init__(self, episodes: EpisodeRepository) -> None:
        self.episodes = episodes
        self.store = episodes.store
        self.agent_id = episodes.agent_id

    def ensure(self, stream_id: str, now: datetime, interval: float, *, activity: bool) -> dict:
        with self.store._transaction() as db:
            self.episodes.scope(db, stream_id)
            row = db.execute(
                "SELECT * FROM organization_schedule WHERE agent_id=?", (self.agent_id,)
            ).fetchone()
            if not row:
                latest = db.execute(
                    "SELECT ended_at FROM runs WHERE conversation_id=? AND ended_at IS NOT NULL "
                    "ORDER BY rowid DESC LIMIT 1",
                    (stream_id,),
                ).fetchone()
                pending = db.execute(
                    "SELECT e.ended_at FROM episodes e JOIN conversations c ON c.id=e.stream_id "
                    "WHERE c.agent_id=? AND e.thread_dirty=1 ORDER BY e.ended_at LIMIT 1",
                    (self.agent_id,),
                ).fetchone()
                base = datetime.fromisoformat(pending[0]) if pending else now
                stamp = now.isoformat() if activity or not latest else latest[0]
                db.execute(
                    "INSERT INTO organization_schedule "
                    "(agent_id,stream_id,last_activity_at,thread_due_at) "
                    "VALUES (?,?,?,?)",
                    (
                        self.agent_id,
                        stream_id,
                        stamp,
                        (base + timedelta(seconds=interval)).isoformat(),
                    ),
                )
            elif activity or row["stream_id"] != stream_id:
                db.execute(
                    "UPDATE organization_schedule SET stream_id=?,last_activity_at=? "
                    "WHERE agent_id=?",
                    (stream_id, now.isoformat(), self.agent_id),
                )
            return dict(
                db.execute(
                    "SELECT * FROM organization_schedule WHERE agent_id=?", (self.agent_id,)
                ).fetchone()
            )

    def state(self, stream_id: str, *, at: datetime | None = None, interval: float = 14400) -> dict:
        with self.store.reading() as db:
            self.episodes.scope(db, stream_id)
            row = db.execute(
                "SELECT * FROM organization_schedule WHERE agent_id=?", (self.agent_id,)
            ).fetchone()
            latest = db.execute(
                "SELECT status,ended_at FROM runs WHERE conversation_id=? "
                "ORDER BY rowid DESC LIMIT 1",
                (stream_id,),
            ).fetchone()
            provisional = db.execute(
                "SELECT ended_at FROM episodes WHERE stream_id=? AND status='provisional' LIMIT 1",
                (stream_id,),
            ).fetchone()
            if row is None:
                now = at or datetime.now(UTC)
                pending = db.execute(
                    "SELECT e.ended_at FROM episodes e JOIN conversations c ON c.id=e.stream_id "
                    "WHERE c.agent_id=? AND e.thread_dirty=1 ORDER BY e.ended_at LIMIT 1",
                    (self.agent_id,),
                ).fetchone()
                base = datetime.fromisoformat(pending[0]) if pending else now
                snapshot = dict(
                    agent_id=self.agent_id,
                    stream_id=stream_id,
                    last_activity_at=(
                        latest["ended_at"] if latest and latest["ended_at"] else now.isoformat()
                    ),
                    thread_due_at=(base + timedelta(seconds=interval)).isoformat(),
                    last_thread_check_at=None,
                    cooldown_until=None,
                    last_cycle_at=None,
                    last_episode_success_at=None,
                    last_thread_success_at=None,
                    last_repair_success_at=None,
                    last_batches=0,
                    last_requests=0,
                    last_error="",
                    cycle_metrics="{}",
                )
            else:
                snapshot = dict(row)
                if snapshot["stream_id"] != stream_id:
                    snapshot.update(
                        stream_id=stream_id,
                        last_activity_at=(at or datetime.now(UTC)).isoformat(),
                    )
            return {
                **snapshot,
                "run_active": bool(latest and latest["status"] == "running"),
                "last_completed_at": latest["ended_at"] if latest else None,
                "provisional_ended_at": provisional[0] if provisional else None,
            }

    def observe(self, stream_id: str) -> dict:
        """Bounded read-only maintenance projection; never creates progress/schedule rows."""
        repository = self.episodes
        with repository.store.reading() as db:
            repository.scope(db, stream_id)
            schedule = db.execute(
                "SELECT * FROM organization_schedule WHERE agent_id=?", (repository.agent_id,)
            ).fetchone()
            progress = repository._progress(db, stream_id)
            pending_runs = db.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM runs WHERE conversation_id=? "
                "AND rowid>? AND status!='running' LIMIT 65)",
                (stream_id, progress["position"]),
            ).fetchone()[0]
            pending_jobs = db.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM memory_learning_jobs WHERE agent_id=? "
                "AND state IN ('pending','processing') LIMIT 65)",
                (repository.agent_id,),
            ).fetchone()[0]
            pending_threads = db.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM topic_threads WHERE agent_id=? "
                "AND dirty=1 LIMIT 65)",
                (repository.agent_id,),
            ).fetchone()[0]
            attention = db.execute(
                "SELECT id,kind,state,error,updated_at FROM memory_learning_jobs "
                "WHERE agent_id=? AND state IN ('failed','requires_reconciliation') "
                "ORDER BY rowid DESC LIMIT 4",
                (repository.agent_id,),
            ).fetchall()
            activation = db.execute(
                "SELECT curator_due_at,updates,curator_updates FROM memory_learning_state "
                "WHERE agent_id=?",
                (repository.agent_id,),
            ).fetchone()
        return {
            "schedule": dict(schedule) if schedule else None,
            "episode_progress": progress,
            "pending_runs_capped": pending_runs,
            "pending_jobs_capped": pending_jobs,
            "dirty_threads_capped": pending_threads,
            "attention": [dict(row) for row in attention],
            "curator": dict(activation) if activation else None,
        }

    def activity(self, stream_id: str, now: datetime) -> None:
        with self.store._transaction() as db:
            self.episodes.scope(db, stream_id)
            db.execute(
                "UPDATE organization_schedule SET last_activity_at=? WHERE agent_id=?",
                (now.isoformat(), self.agent_id),
            )

    def thread_check(self, now: datetime, interval: float, *, success: bool = False) -> None:
        with self.store._transaction() as db:
            db.execute(
                "UPDATE organization_schedule SET last_thread_check_at=?,thread_due_at=?,"
                "last_thread_success_at=CASE WHEN ? THEN ? ELSE last_thread_success_at END "
                "WHERE agent_id=?",
                (
                    now.isoformat(),
                    (now + timedelta(seconds=interval)).isoformat(),
                    success,
                    now.isoformat(),
                    self.agent_id,
                ),
            )

    def cycle(
        self,
        now: datetime,
        batches: int,
        requests: int,
        outcome: str,
        successes: frozenset[str],
        cooldown: float,
        metrics: dict | None = None,
    ) -> None:
        with self.store._transaction() as db:
            db.execute(
                "UPDATE organization_schedule SET last_cycle_at=?,last_batches=?,last_requests=?,"
                "last_error=?,cooldown_until=?,cycle_metrics=?,"
                "last_episode_success_at=CASE WHEN ? THEN ? ELSE last_episode_success_at END,"
                "last_repair_success_at=CASE WHEN ? THEN ? ELSE last_repair_success_at END "
                "WHERE agent_id=?",
                (
                    now.isoformat(),
                    batches,
                    requests,
                    outcome,
                    (now + timedelta(seconds=cooldown)).isoformat(),
                    json.dumps(metrics or {}),
                    "episode" in successes,
                    now.isoformat(),
                    "repair" in successes,
                    now.isoformat(),
                    self.agent_id,
                ),
            )
