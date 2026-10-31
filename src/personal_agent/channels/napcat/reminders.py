"""Explicit reminders only; no proactive chat sender or arbitrary OneBot action surface."""

import asyncio

from personal_agent.channels.napcat.transport import NapCatTransport
from personal_agent.channels.qq.config import QQError
from personal_agent.proactive.delivery import DeliveryError, SendReceipt


class NapCatReminderSender:
    mode = "napcat"

    def __init__(self, transport: NapCatTransport) -> None:
        self.transport = transport
        self._next_send = 0.0

    async def send(self, recipient: str, text: str, idempotency_key: str) -> SendReceipt:
        if not idempotency_key.startswith("scheduled:"):
            raise DeliveryError(uncertain=False)
        socket = self.transport.socket
        loop = asyncio.get_running_loop()
        delay = self._next_send - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        if socket is None or self.transport.socket is not socket or self.transport.state != "ready":
            raise DeliveryError(uncertain=False)
        try:
            receipt = await self.transport.send_reminder(recipient, "提醒：" + text)
        except QQError as error:
            raise DeliveryError(uncertain=error.uncertain) from None
        finally:
            self._next_send = loop.time() + self.transport.policy.qq_send_interval_seconds
        return SendReceipt(receipt_id=receipt)
