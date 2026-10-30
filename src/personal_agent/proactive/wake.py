"""One persisted-deadline coordinator, driven by the application's existing timer."""

from datetime import datetime, timedelta, timezone

from personal_agent.proactive.models import ProactivePolicy
from personal_agent.storage.proactive import ProactiveRepository


class WakeCoordinator:
    def __init__(self, repository: ProactiveRepository, policy: ProactivePolicy) -> None:
        self.repository, self.policy = repository, policy

    def quiet_until(self, now: datetime) -> datetime | None:
        policy = self.policy
        local = now.astimezone(timezone(timedelta(minutes=policy.utc_offset_minutes)))
        start, end = policy.quiet_start_hour, policy.quiet_end_hour
        quiet = (
            (start <= local.hour < end if start < end else local.hour >= start or local.hour < end)
            if start != end
            else False
        )
        if not quiet:
            return None
        until = local.replace(hour=end, minute=0, second=0, microsecond=0)
        if until <= local:
            until += timedelta(days=1)
        return until

    def delay(self, now: datetime) -> float:
        # Poll new journal/thread metadata at most once a minute. No per-item timers.
        if not self.policy.enabled:
            return 60
        if self.repository.pending():
            return 1
        with self.repository.store.reading() as db:
            row = db.execute(
                "SELECT MIN(COALESCE(next_review_at,expires_at)) FROM proactive_proposals "
                "WHERE agent_id=? AND status IN ('open','deferred')",
                (self.repository.agent_id,),
            ).fetchone()
        if not row[0]:
            return 60
        due = datetime.fromisoformat(row[0])
        quiet = self.quiet_until(now)
        if quiet:
            due = max(due, quiet)
        return min(60, max(1, (due - now).total_seconds()))
