"""One bounded timer for explicit reminders, independent of Attention and model calls."""

import asyncio
from collections.abc import Callable
from datetime import datetime

from personal_agent.core.evidence import SECRET
from personal_agent.core.history import HistoryError
from personal_agent.organization.clock import Clock, SystemClock
from personal_agent.proactive.delivery import DeliveryError, Sender, SendReceipt
from personal_agent.scheduler.models import (
    ReminderError,
    ReminderGrant,
    ReminderRequest,
    SchedulerPolicy,
)
from personal_agent.storage.reminders import ReminderRepository


class SchedulerRuntime:
    def __init__(
        self,
        repository: ReminderRepository,
        policy: SchedulerPolicy,
        *,
        clock: Clock | None = None,
        read_only: bool = False,
    ) -> None:
        self.repository, self.policy = repository, policy
        self.clock = clock or SystemClock()
        self.read_only = read_only
        self.task: asyncio.Task | None = None
        self.wake = asyncio.Event()
        self._closed = False
        self._recovering = not read_only
        self._sending: set[asyncio.Task] = set()
        self.error_code = ""
        self.sender: Sender | None = None
        self.delivery_grant: Callable[[], ReminderGrant | None] | None = None
        self.connected_at: datetime | None = None

    def bind_delivery(self, sender: Sender, grant: Callable[[], ReminderGrant | None]) -> None:
        if self.read_only or self._closed or sender.mode != "napcat":
            raise ReminderError("scheduler_delivery_unavailable")
        self.sender, self.delivery_grant = sender, grant
        self.connected_at = self.clock.now()
        self.wake.set()

    def unbind_delivery(self) -> None:
        self.sender, self.delivery_grant, self.connected_at = None, None, None
        self.wake.set()

    def delivery_available(self) -> bool:
        grant = self.delivery_grant() if self.delivery_grant is not None else None
        return bool(
            not self.read_only
            and not self._closed
            and self.policy.enabled
            and not self.error_code
            and self.sender is not None
            and self.sender.mode == "napcat"
            and grant is not None
            and grant.enabled
            and grant.channel == "napcat"
            and grant.source_scope is not None
            and grant.source_scope.startswith("chat:")
        )

    def delivery_for(self, conversation: str, scope: str) -> bool:
        if not self.delivery_available():
            return False
        grant = self.delivery_grant()
        return bool(grant and grant.conversation_id == conversation and grant.source_scope == scope)

    def _require_write(self) -> None:
        if self.read_only or self._closed or not self.policy.enabled or self.error_code:
            raise ReminderError("scheduler_unavailable")

    def create(self, request: ReminderRequest) -> dict:
        self._require_write()
        item = self.repository.create(request, self.policy, self.clock.now())
        self.wake.set()
        return dict(
            outcome="recorded",
            reminder=item,
            delivery_available=self.delivery_for(request.conversation_id, request.source_scope),
            notice="提醒已记录；仅渠道可用且排期有效时尝试投递，不代表已送达。",
        )

    def cancel(self, reminder_id: str) -> dict:
        # Cancelling a queued item stays available when the timer is disabled or failed.
        if self.read_only or self._closed:
            raise ReminderError("scheduler_unavailable")
        result = self.repository.cancel(reminder_id, self.clock.now())
        self.wake.set()
        return result

    def tick(self) -> dict:
        self._require_write()
        recovered = 0
        if self._recovering:
            recovered = self.repository.recover(self.clock.now(), self.policy.batch_size)
            self._recovering = recovered == self.policy.batch_size
        changed = self.repository.advance(self.clock.now(), self.policy.batch_size)
        return dict(
            outcome="checked",
            recovered=recovered,
            changed=changed,
            delivery_available=self.delivery_available(),
        )

    def start(self) -> None:
        if not self.read_only and self.policy.enabled and not self._closed and self.task is None:
            self.task = asyncio.create_task(self._background(), name="explicit-reminders")

    async def _wait(self, delay: float) -> None:
        sleeper = asyncio.create_task(self.clock.sleep(delay))
        signal = asyncio.create_task(self.wake.wait())
        try:
            done, _ = await asyncio.wait([sleeper, signal], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in (sleeper, signal):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleeper, signal, return_exceptions=True)

    async def _background(self) -> None:
        try:
            while not self._closed and self.policy.enabled:
                self.wake.clear()
                if self._recovering:
                    self.tick()
                await self.deliver_due()
                self.tick()
                delay = (
                    0.05
                    if self._recovering
                    else self.repository.next_delay(self.clock.now(), self.policy.check_seconds)
                )
                await self._wait(delay)
        except asyncio.CancelledError:
            raise
        except (HistoryError, ReminderError):
            self.error_code = "scheduler_storage_unavailable"
        except Exception:
            self.error_code = "scheduler_runtime_error"

    async def deliver_due(self) -> int:
        self._require_write()
        if self._recovering or not self.delivery_available():
            return 0
        sender, grant_provider = self.sender, self.delivery_grant
        if sender is None or grant_provider is None or self.connected_at is None:
            return 0
        grant = grant_provider()
        if grant is None or not grant.enabled or grant.channel != "napcat":
            return 0
        items = self.repository.delivery_due(
            self.clock.now(), grant, self.connected_at, self.policy.batch_size
        )
        sent = 0
        for item in items:
            current = grant_provider()
            if (
                self.sender is not sender
                or self.delivery_grant is not grant_provider
                or current != grant
                or sender.mode != "napcat"
                or not self.delivery_available()
            ):
                break
            if self.repository.claim(item, current, self.clock.now()):
                task = asyncio.current_task()
                if task is not None:
                    self._sending.add(task)
                try:
                    await self._send(item, sender, current)
                finally:
                    if task is not None:
                        self._sending.discard(task)
                sent += 1
        return sent

    async def simulate(
        self, reminder_id: str, sender: Sender, grant: Callable[[], ReminderGrant | None]
    ) -> dict:
        self._require_write()
        initial = grant()
        if (
            self._recovering
            or sender.mode != "simulation"
            or initial is None
            or not initial.enabled
            or initial.channel != "simulation"
        ):
            return dict(outcome="blocked", reason="simulation_permission_or_recovery")
        item = self.repository.get(reminder_id)
        current = grant()
        if current != initial or not current.enabled or sender.mode != "simulation":
            return dict(outcome="blocked", reason="grant_changed")
        self._require_write()
        if not self.repository.claim(item, current, self.clock.now()):
            return dict(outcome="blocked", reason="not_due_scope_or_terminal")
        task = asyncio.current_task()
        if task is not None:
            self._sending.add(task)
        try:
            return await self._send(item, sender, current)
        finally:
            if task is not None:
                self._sending.discard(task)

    async def _send(self, item: dict, sender: Sender, grant: ReminderGrant) -> dict:
        try:
            async with asyncio.timeout(self.policy.timeout_seconds) as deadline:
                receipt = await sender.send(
                    grant.recipient, item["text"], "scheduled:" + item["id"]
                )
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise asyncio.CancelledError
                receipt = SendReceipt.model_validate(receipt)
                if deadline.expired() or SECRET.search(receipt.receipt_id):
                    raise DeliveryError()
        except asyncio.CancelledError:
            self.repository.finish(item["id"], "uncertain", self.clock.now(), code="send_cancelled")
            raise
        except DeliveryError as error:
            status = "uncertain" if error.uncertain else "failed"
            self.repository.finish(item["id"], status, self.clock.now(), code="send_" + status)
            return dict(outcome=status, mode=grant.channel)
        except Exception:
            self.repository.finish(
                item["id"], "uncertain", self.clock.now(), code="send_unconfirmed"
            )
            return dict(outcome="uncertain", mode=grant.channel)
        # A receipt transaction failure leaves sending. Recovery must not replay its effect.
        self.repository.finish(
            item["id"], "accepted", self.clock.now(), receipt_id=receipt.receipt_id
        )
        return dict(outcome="accepted", mode=grant.channel, reminder_id=item["id"])

    async def close(self) -> None:
        self._closed = True
        self.unbind_delivery()
        tasks = [
            task
            for task in [self.task, *self._sending]
            if task is not None and task is not asyncio.current_task()
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.task = None

    def status(self) -> dict:
        return dict(
            enabled=self.policy.enabled,
            read_only=self.read_only,
            running=bool(self.task and not self.task.done()),
            recovering=self._recovering,
            error_code=self.error_code,
            delivery_available=self.delivery_available(),
        )
