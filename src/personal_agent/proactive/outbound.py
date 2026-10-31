"""Draft generation and explicitly authorized local simulation, with final Host checks."""

import asyncio
from collections.abc import Callable

from personal_agent.core.evidence import SECRET
from personal_agent.core.prompts import CONVERSATION_PROMPT
from personal_agent.organization.models import OrganizationError
from personal_agent.proactive.composer import Composer
from personal_agent.proactive.delivery import DeliveryError, DeliveryGrant, Sender, SendReceipt
from personal_agent.proactive.runtime import ProactiveRuntime
from personal_agent.storage.outbox import OutboxRepository
from personal_agent.storage.proactive import stamp


class OutboundRuntime:
    def __init__(
        self,
        proactive: ProactiveRuntime,
        repository: OutboxRepository,
        conversation: Callable[[], str],
        persona: Callable[[], tuple[str, list[dict]]],
        *,
        initialize: bool = True,
    ) -> None:
        self.proactive, self.repository = proactive, repository
        self.conversation, self.persona = conversation, persona
        self.composer = Composer(proactive.model)
        if initialize:
            repository.recover(proactive.clock.now())

    def ready(self) -> bool:
        return bool(self.proactive.policy.compose_enabled and self.repository.next_decision())

    def _valid(self, item: dict) -> bool:
        fingerprint, _ = self.persona()
        return self.proactive.gate() == "eligible" and self.repository.valid(
            item,
            self.proactive.clock.now(),
            self.proactive.repository.activity(),
            self.proactive.activity_epoch,
            self.conversation(),
            fingerprint,
        )

    async def compose(self, retry_id: str | None = None) -> dict:
        if not self.proactive.policy.compose_enabled or self.proactive.gate() != "eligible":
            return dict(outcome="no_outbound_work")
        at = self.proactive.clock.now()
        fingerprint, style = self.persona()
        if retry_id is not None:
            item = self.repository.restart(
                retry_id,
                self.conversation(),
                self.proactive.repository.activity(),
                self.proactive.activity_epoch,
                fingerprint,
                at,
            )
        else:
            decision = self.repository.next_decision()
            if not decision:
                return dict(outcome="no_outbound_work")
            item = self.repository.begin(
                decision,
                self.conversation(),
                self.proactive.repository.activity(),
                self.proactive.activity_epoch,
                fingerprint,
                at,
            )
        if item["status"] != "composing":
            return dict(outcome="stale", outbox_id=item["id"])
        proposal = self.repository.proposal(item["proposal_id"], item["proposal_revision"], at)
        try:
            draft = await self.composer.compose(
                {
                    key: proposal[key]
                    for key in ("id", "topic_key", "title", "context", "sources", "followup_window")
                },
                [
                    {**part, "text": part["text"].removesuffix("\n\n" + CONVERSATION_PROMPT)}
                    for part in style
                ],
                stamp(at),
            )
        except asyncio.CancelledError:
            self.repository.stop(
                item["id"], "compose_failed", "compose_cancelled", self.proactive.clock.now()
            )
            raise
        except OrganizationError as error:
            self.repository.stop(
                item["id"], "compose_failed", error.code, self.proactive.clock.now()
            )
            return dict(outcome="failed", error_code=error.code, outbox_id=item["id"])
        if not self._valid(item):
            self.repository.stop(item["id"], "cancelled", "draft_stale", self.proactive.clock.now())
            return dict(outcome="stale", outbox_id=item["id"])
        saved = self.repository.prepare(item, draft.text, self.proactive.clock.now())
        if not saved:
            self.repository.stop(item["id"], "cancelled", "draft_stale", self.proactive.clock.now())
        return dict(
            outcome="completed" if saved else "stale",
            stage="composer",
            mode="preview",
            outbox_id=item["id"],
        )

    async def simulate(
        self, item_id: str, sender: Sender, grant: Callable[[], DeliveryGrant | None]
    ) -> dict:
        # Only a trusted caller may provide the local fake and grant. No model/HTTP path.
        initial = grant()
        if sender.mode != "simulation" or initial is None or not initial.enabled:
            return dict(outcome="blocked", reason="simulation_permission_required")
        item = self.repository.get(item_id)
        if item["status"] != "prepared":
            return dict(outcome="blocked", reason="outbox_not_prepared")
        if not self._valid(item):
            self.repository.stop(item_id, "cancelled", "draft_stale", self.proactive.clock.now())
            return dict(outcome="stale")
        current = grant()
        if current != initial or not current.enabled:
            return dict(outcome="blocked", reason="grant_changed")
        if not self._valid(item):
            self.repository.stop(item_id, "cancelled", "draft_stale", self.proactive.clock.now())
            return dict(outcome="stale")
        if not self.repository.claim(item, current, self.proactive.clock.now()):
            return dict(outcome="blocked", reason="scope_rate_or_revision")
        try:
            # Claim/attempt are committed before the first await. Cancellation is uncertain.
            async with asyncio.timeout(self.proactive.policy.timeout_seconds) as deadline:
                receipt = await sender.send(current.recipient, item["text"], item_id)
                if deadline.expired():
                    raise DeliveryError()
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise asyncio.CancelledError
                receipt = SendReceipt.model_validate(receipt)
                if SECRET.search(receipt.receipt_id):
                    raise DeliveryError()
        except asyncio.CancelledError:
            self.repository.finish(
                item_id, "uncertain", self.proactive.clock.now(), code="send_cancelled"
            )
            raise
        except DeliveryError as error:
            status = "uncertain" if error.uncertain else "failed"
            self.repository.finish(
                item_id,
                status,
                self.proactive.clock.now(),
                code="send_uncertain" if error.uncertain else "send_failed",
            )
            return dict(outcome=status, mode="simulation")
        except Exception:
            self.repository.finish(
                item_id, "uncertain", self.proactive.clock.now(), code="send_unconfirmed"
            )
            return dict(outcome="uncertain", mode="simulation")
        # Storage failure here cannot undo sender effects. Leave sending for recovery.
        self.repository.finish(
            item_id, "accepted", self.proactive.clock.now(), receipt=receipt.receipt_id
        )
        return dict(outcome="accepted", mode="simulation", outbox_id=item_id)
