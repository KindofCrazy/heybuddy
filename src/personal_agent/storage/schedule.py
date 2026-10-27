"""One Agent-owned schedule row; short metadata-only transactions, no model requests."""

from datetime import datetime, timedelta

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

    def state(self, stream_id: str) -> dict:
        with self.store._transaction() as db:
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
            return {
                **dict(row),
                "run_active": bool(latest and latest["status"] == "running"),
                "last_completed_at": latest["ended_at"] if latest else None,
                "provisional_ended_at": provisional[0] if provisional else None,
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
    ) -> None:
        with self.store._transaction() as db:
            db.execute(
                "UPDATE organization_schedule SET last_cycle_at=?,last_batches=?,last_requests=?,"
                "last_error=?,cooldown_until=?,"
                "last_episode_success_at=CASE WHEN ? THEN ? ELSE last_episode_success_at END,"
                "last_repair_success_at=CASE WHEN ? THEN ? ELSE last_repair_success_at END "
                "WHERE agent_id=?",
                (
                    now.isoformat(),
                    batches,
                    requests,
                    outcome,
                    (now + timedelta(seconds=cooldown)).isoformat(),
                    "episode" in successes,
                    now.isoformat(),
                    "repair" in successes,
                    now.isoformat(),
                    self.agent_id,
                ),
            )
