"""Explicit local simulation transport; no production channel adapter is registered."""

from typing import Literal, Protocol

from pydantic import ConfigDict, Field

from personal_agent.core.models import Model


class DeliveryGrant(Model):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = False
    revision: int = Field(default=1, ge=1)
    conversation_id: str = Field(min_length=1, max_length=128)
    recipient: str = Field(min_length=1, max_length=128)
    channel: Literal["simulation"] = "simulation"
    cooldown_seconds: int = Field(default=21600, ge=0, le=604800)
    daily_limit: int = Field(default=2, ge=1, le=20)


class SendReceipt(Model):
    receipt_id: str = Field(min_length=1, max_length=128)
    accepted: Literal[True] = True


class DeliveryError(Exception):
    def __init__(self, *, uncertain: bool = True):
        super().__init__("simulation_send_uncertain" if uncertain else "simulation_send_failed")
        self.uncertain = uncertain


class Sender(Protocol):
    mode: str

    async def send(self, recipient: str, text: str, idempotency_key: str) -> SendReceipt: ...


class FakeSender:
    """In-memory fake, explicitly marked simulation; never reaches any network."""

    mode = "simulation"

    def __init__(self) -> None:
        self.messages: dict[str, tuple[str, str]] = {}

    async def send(self, recipient: str, text: str, idempotency_key: str) -> SendReceipt:
        previous = self.messages.get(idempotency_key)
        if previous is not None and previous != (recipient, text):
            raise DeliveryError(uncertain=False)
        self.messages[idempotency_key] = (recipient, text)
        return SendReceipt(receipt_id=idempotency_key)
