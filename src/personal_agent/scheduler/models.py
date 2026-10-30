from typing import Literal

from pydantic import AwareDatetime, ConfigDict, Field, field_validator

from personal_agent.core.models import Model


class ReminderError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SchedulerPolicy(Model):
    enabled: bool = True
    check_seconds: float = Field(default=30, gt=0, le=300, allow_inf_nan=False)
    batch_size: int = Field(default=16, ge=1, le=64)
    grace_seconds: int = Field(default=86400, ge=1, le=604800)
    max_future_seconds: int = Field(default=31622400, ge=60, le=31622400)
    timeout_seconds: float = Field(default=10, gt=0, le=60, allow_inf_nan=False)


class ReminderRequest(Model):
    request_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=128)
    due_at: AwareDatetime
    text: str = Field(min_length=1, max_length=1200)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Blank reminder")
        return value.strip()


class ReminderGrant(Model):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = False
    revision: int = Field(default=1, ge=1)
    conversation_id: str = Field(min_length=1, max_length=128)
    recipient: str = Field(min_length=1, max_length=128)
    channel: Literal["simulation"] = "simulation"
