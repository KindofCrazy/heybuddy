"""Draft generation and Host-bound delivery, with final provenance and activity checks."""

import asyncio
from collections.abc import Callable
from datetime import datetime

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
        self.sender: Sender | None = None
        self.delivery_grant: Callable[[], DeliveryGrant | None] | None = None
        self.connected_at: datetime | None = None
        self.read_only = not initialize
        if initialize:
            repository.recover(proactive.clock.now())

    def ready(self) -> bool:
        return bool(
            self.proactive.policy.compose_enabled
            and (self.repository.next_decision() or self._next_delivery())
        )

    def bind_delivery(self, sender: Sender, grant: Callable[[], DeliveryGrant | None]) -> None:
        if self.read_only or sender.mode != "napcat":
            raise ValueError("Proactive delivery unavailable")
        self.sender, self.delivery_grant = sender, grant
        self.connected_at = self.proactive.clock.now()

    def unbind_delivery(self) -> None:
        self.sender, self.delivery_grant, self.connected_at = None, None, None

    def delivery_available(self) -> bool:
        grant = self.delivery_grant() if self.delivery_grant else None
        return bool(
            not self.read_only
            and self.proactive.policy.enabled
            and self.proactive.policy.compose_enabled
            and self.sender is not None
            and self.sender.mode == "napcat"
            and self.connected_at is not None
            and grant is not None
            and grant.enabled
            and grant.channel == "napcat"
            and grant.source_scope
            and grant.channel_scope
            and grant.conversation_id == self.conversation()
        )

    def _next_delivery(self) -> dict | None:
        if not self.delivery_available():
            return None
        return self.repository.next_prepared(self.conversation(), self.connected_at)

    def delivery_allowed(self, item_id: str) -> bool:
        if not self.delivery_available():
            return False
        grant = self.delivery_grant()
        item = self.repository.get(item_id)
        return bool(
            item["status"] == "sending"
            and item["recipient"] == grant.recipient
            and item["grant_revision"] == grant.revision
            and item["source_scope"] == grant.source_scope
            and item["created_at"] >= stamp(self.connected_at)
            and self._valid(item)
            and self.repository.delivery_scope(item, grant, self.proactive.clock.now())
        )

    async def run(self) -> dict:
        item = self._next_delivery()
        if item is None:
            return await self.compose()
        sender, grant = self.sender, self.delivery_grant
        result = await self._deliver(item["id"], sender, grant, mode="napcat")
        if result["outcome"] == "blocked":
            self.repository.stop(
                item["id"], "cancelled", "delivery_blocked", self.proactive.clock.now()
            )
        return result

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
        return await self._deliver(item_id, sender, grant, mode="simulation")

    async def _deliver(
        self,
        item_id: str,
        sender: Sender,
        grant: Callable[[], DeliveryGrant | None],
        *,
        mode: str,
    ) -> dict:
        # Only a trusted Host binds a transport; no model/HTTP delivery entry point.
        initial = grant()
        if (
            self.read_only
            or sender.mode != mode
            or initial is None
            or not initial.enabled
            or initial.channel != mode
        ):
            return dict(outcome="blocked", reason="delivery_permission_required")
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
                key = "proactive:" + item_id if mode == "napcat" else item_id
                receipt = await sender.send(current.recipient, item["text"], key)
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
            return dict(outcome=status, mode=mode)
        except Exception:
            self.repository.finish(
                item_id, "uncertain", self.proactive.clock.now(), code="send_unconfirmed"
            )
            return dict(outcome="uncertain", mode=mode)
        # Storage failure here cannot undo sender effects. Leave sending for recovery.
        self.repository.finish(
            item_id, "accepted", self.proactive.clock.now(), receipt=receipt.receipt_id
        )
        return dict(outcome="accepted", mode=mode, outbox_id=item_id)
