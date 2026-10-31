"""Bounded proactive units in the Host's serial, preemptible maintenance lane."""

from typing import TYPE_CHECKING

from personal_agent.core.history import HistoryError
from personal_agent.organization.clock import Clock, SystemClock
from personal_agent.organization.models import OrganizationError
from personal_agent.organization.provider import OrganizerModel
from personal_agent.proactive.attention import AttentionEngine
from personal_agent.proactive.models import (
    AttentionDecision,
    ProactivePolicy,
    ScoutResult,
    followup_targets,
)
from personal_agent.proactive.scout import OpportunityScout
from personal_agent.proactive.wake import WakeCoordinator
from personal_agent.storage.proactive import ProactiveRepository, stamp

if TYPE_CHECKING:
    from personal_agent.proactive.outbound import OutboundRuntime


class ProactiveRuntime:
    def __init__(
        self,
        repository: ProactiveRepository,
        model: OrganizerModel,
        policy: ProactivePolicy,
        *,
        clock: Clock | None = None,
        initialize: bool = True,
    ) -> None:
        self.repository, self.model, self.policy = repository, model, policy
        self.clock = clock or SystemClock()
        self.activity_epoch = 0
        self.outbound: OutboundRuntime | None = None
        self.scout = OpportunityScout(model)
        self.attention = AttentionEngine(model)
        self.wake = WakeCoordinator(repository, policy)
        if initialize and policy.enabled:
            repository.initialize()

    def gate(self) -> str:
        if not self.policy.enabled:
            return "disabled"
        state = self.repository.state()
        if not state:
            return "uninitialized"
        if state["error_code"]:
            return "failed"
        if self.repository.activity()[2]:
            return "foreground"
        if self.repository.pending():
            return "unprocessed_evidence"
        if self.wake.quiet_until(self.clock.now()):
            return "quiet_hours"
        return "eligible"

    def ready(self) -> bool:
        if not self.policy.enabled:
            return False
        if self.repository.has_expired(self.clock.now()):
            return True
        gate = self.gate()
        if gate == "unprocessed_evidence":
            return True
        return gate == "eligible" and bool(
            self.repository.due(self.clock.now(), 1) or (self.outbound and self.outbound.ready())
        )

    def status(self) -> dict:
        return dict(
            mode="shadow",
            enabled=self.policy.enabled,
            gate=self.gate(),
            state=self.repository.state(),
            next_check_seconds=self.wake.delay(self.clock.now()),
            outbound_enabled=False,
            compose_enabled=self.policy.compose_enabled,
        )

    async def run(self) -> dict:
        if not self.policy.enabled:
            return dict(outcome="disabled")
        at = self.clock.now()
        expired = self.repository.expire(at)
        gate = self.gate()
        if gate == "unprocessed_evidence":
            snapshot = self.repository.snapshot(self.policy)
            if snapshot is None:
                return dict(outcome="no_proactive_work")
            if snapshot["lane"] == "run_cursor" and snapshot["skip"] == "source_budget":
                self.repository.fail("source_budget", at)
                return dict(outcome="blocked", error_code="source_budget")
            try:
                result = ScoutResult()
                if snapshot["users"] and not snapshot["skip"]:
                    existing = self.repository.candidates()
                    snapshot["review_targets"] = followup_targets(existing)
                    result = await self.scout.extract(snapshot, existing, stamp(at))
                self.repository.publish(snapshot, result, self.policy, at)
                return dict(outcome="completed", stage="scout", changes=len(result.changes))
            except OrganizationError as error:
                if error.code in {"maintenance_budget", "maintenance_request_budget"}:
                    return dict(outcome="budget_exhausted")
                self.repository.fail(error.code, at)
                return dict(outcome="failed", error_code=error.code)
            except HistoryError:
                # Publication rolled back. Freeze this cursor until trusted explicit retry.
                self.repository.fail("publication_failed", at)
                return dict(outcome="failed", error_code="publication_failed")
        if expired:
            return dict(outcome="completed", stage="expiry", expired=expired)
        if gate != "eligible":
            return dict(outcome="no_proactive_work", gate=gate)
        if self.outbound is not None and self.outbound.ready():
            return await self.outbound.compose()
        proposals = self.repository.due(at, self.policy.candidate_limit)
        if not proposals:
            return dict(outcome="no_proactive_work")
        activity = self.repository.activity()
        epoch = self.activity_epoch
        try:
            decision = await self.attention.evaluate(proposals, stamp(at))
        except OrganizationError as error:
            if error.code in {"maintenance_budget", "maintenance_request_budget"}:
                return dict(outcome="budget_exhausted")
            if self.gate() != "eligible" or self.activity_epoch != epoch:
                return dict(outcome="stale", stage="attention")
            decision = AttentionDecision(
                action="silent", proposal_ids=[p["id"] for p in proposals], reason=error.code
            )
            self.repository.decide(
                proposals, decision, activity, self.clock.now(), self.policy, failed=True
            )
            return dict(outcome="failed", error_code=error.code)
        # A foreground receipt can preempt this coroutine. Also recheck latest durable input.
        if self.gate() != "eligible" or self.activity_epoch != epoch:
            return dict(outcome="stale", stage="attention")
        saved = self.repository.decide(proposals, decision, activity, self.clock.now(), self.policy)
        return dict(outcome="completed" if saved else "stale", stage="attention", mode="shadow")

    def retry(self) -> dict:
        if not self.policy.enabled:
            return dict(outcome="disabled")
        self.repository.retry(self.clock.now())
        return dict(outcome="ready")
