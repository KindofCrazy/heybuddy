"""One private check-in to the bound peer, with a final check after rate-limit waits."""

import asyncio
from collections.abc import Callable

from personal_agent.channels.napcat.transport import NapCatTransport
from personal_agent.channels.qq.config import QQError
from personal_agent.proactive.delivery import DeliveryError, SendReceipt


class NapCatProactiveSender:
    mode = "napcat"

    def __init__(self, transport: NapCatTransport, permitted: Callable[[str], bool]) -> None:
        self.transport, self.permitted = transport, permitted
        self._next_send = 0.0

    async def send(self, recipient: str, text: str, idempotency_key: str) -> SendReceipt:
        if not idempotency_key.startswith("proactive:"):
            raise DeliveryError(uncertain=False)
        item_id = idempotency_key.removeprefix("proactive:")
        socket = self.transport.socket
        loop = asyncio.get_running_loop()
        delay = self._next_send - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        if (
            socket is None
            or self.transport.socket is not socket
            or self.transport.state != "ready"
            or not self.permitted(item_id)
        ):
            raise DeliveryError(uncertain=False)
        try:
            receipt = await self.transport.send_proactive(recipient, text)
        except QQError as error:
            raise DeliveryError(uncertain=error.uncertain) from None
        finally:
            self._next_send = loop.time() + self.transport.policy.qq_send_interval_seconds
        return SendReceipt(receipt_id=receipt)
