"""Host policy, bounded source evidence and tool-free model suggestions."""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from personal_agent.core.evidence import Evidence
from personal_agent.core.models import Model


class ProactivePolicy(Model):
    enabled: bool = True
    compose_enabled: bool = True
    input_tokens: int = Field(default=16384, ge=4096, le=65536)
    output_tokens: int = Field(default=4096, ge=256, le=16384)
    timeout_seconds: float = Field(default=30, gt=0, le=120, allow_inf_nan=False)
    source_bytes: int = Field(default=32768, ge=1024, le=65536)
    review_seconds: int = Field(default=21600, ge=60, le=604800)
    lifetime_seconds: int = Field(default=604800, ge=3600, le=2592000)
    quiet_start_hour: int = Field(default=23, ge=0, le=23)
    quiet_end_hour: int = Field(default=8, ge=0, le=23)
    utc_offset_minutes: int = Field(default=480, ge=-720, le=840)
    candidate_limit: int = Field(default=3, ge=1, le=4)
    social_followup_enabled: bool = True
    evening_start_minute: int = Field(default=1140, ge=0, le=1438)
    evening_end_minute: int = Field(default=1290, ge=1, le=1440)
    before_probability: float = Field(default=0.3, ge=0, le=1, allow_inf_nan=False)
    before_min_minutes: int = Field(default=15, ge=1, le=120)
    before_max_minutes: int = Field(default=45, ge=2, le=180)

    @model_validator(mode="after")
    def windows(self):
        if self.evening_start_minute >= self.evening_end_minute:
            raise ValueError("Invalid evening window")
        if self.before_min_minutes >= self.before_max_minutes:
            raise ValueError("Invalid pre-event window")
        return self


class UserSource(Model):
    conversation_id: str
    run_id: str
    sequence: int
    text: str
    position: int
    ended_at: AwareDatetime


class ProposalChange(Model):
    topic_key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=120)
    context: str = Field(min_length=1, max_length=1000)
    status: Literal["open", "resolved", "dismissed"] = "open"
    sources: list[Evidence] = Field(min_length=1, max_length=4)
    next_review_at: AwareDatetime | None = None


class ScoutResult(Model):
    changes: list[ProposalChange] = Field(default_factory=list, max_length=4)


class AttentionDecision(Model):
    action: Literal["silent", "defer", "notify", "propose_task"]
    proposal_ids: list[str] = Field(min_length=1, max_length=4)
    reason: str = Field(min_length=1, max_length=1000)
    next_review_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.proposal_ids)) != len(self.proposal_ids):
            raise ValueError("Duplicate proposals")
        return self
