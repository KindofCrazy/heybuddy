"""Strict bounded suggestions, source snapshots and derived temporal records."""

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from personal_agent.core.history import HistoryTurn
from personal_agent.core.models import Model


class OrganizationError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(f"Organization stopped ({code}); original history was retained.")
        self.code = code


class OrganizationPolicy(Model):
    enabled: bool = True
    page_runs: int = Field(default=8, ge=1, le=16)
    input_tokens: int = Field(default=24576, ge=4096, le=131072)
    output_tokens: int = Field(default=8192, ge=256, le=1_000_000)
    response_bytes: int = Field(default=32768, ge=1024, le=65536)
    source_bytes: int = Field(default=65536, ge=1024, le=262144)
    timeout_seconds: float = Field(default=60, gt=0, le=300, allow_inf_nan=False)
    idle_seconds: float = Field(default=1500, gt=0, le=86400, allow_inf_nan=False)
    run_threshold: int = Field(default=8, ge=1, le=64)
    weak_gap_seconds: int = Field(default=900, ge=1)
    strong_gap_seconds: int = Field(default=5400, ge=1)
    split_gap_seconds: int = Field(default=10800, ge=1)
    revision_seconds: int = Field(default=10800, ge=1, le=86400)
    thread_threshold: int = Field(default=3, ge=2, le=16)
    thread_interval_seconds: float = Field(default=14400, gt=0, le=604800, allow_inf_nan=False)
    check_seconds: float = Field(default=60, gt=0, le=3600, allow_inf_nan=False)
    cooldown_seconds: float = Field(default=60, gt=0, le=86400, allow_inf_nan=False)
    maintenance_batches: int = Field(default=4, ge=1, le=32)
    maintenance_requests: int = Field(default=4, ge=1, le=32)
    maintenance_tokens: int = Field(default=131072, ge=4096, le=1048576)
    maintenance_seconds: float = Field(default=120, gt=0, le=600, allow_inf_nan=False)
    repair_retry_seconds: float = Field(default=300, gt=0, le=86400, allow_inf_nan=False)
    repair_max_failures: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def ordered(self):
        if not self.weak_gap_seconds < self.strong_gap_seconds < self.split_gap_seconds:
            raise ValueError("Temporal thresholds must increase")
        return self


class EvidenceRef(Model):
    conversation_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=512)


class Highlight(Model):
    kind: Literal["event", "decision", "open_loop", "correction"]
    content: str = Field(min_length=1, max_length=1000)
    attribution: Literal["user_confirmed", "assistant_suggestion", "uncertain"]
    sources: list[EvidenceRef] = Field(min_length=1, max_length=4)


class Segment(Model):
    run_ids: list[str] = Field(min_length=1, max_length=16)
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=2000)
    highlights: list[Highlight] = Field(default_factory=list, max_length=6)


class Segmentation(Model):
    episodes: list[Segment] = Field(min_length=1, max_length=16)


class Episode(Segment):
    id: str
    stream_id: str
    started_at: AwareDatetime
    ended_at: AwareDatetime
    thread_id: str | None = None
    status: Literal["provisional", "finalized"]
    revision: int = Field(ge=1)
    thread_dirty: bool = True

    @model_validator(mode="after")
    def times(self):
        if self.started_at > self.ended_at:
            raise ValueError("Episode time range is reversed")
        return self


class Source(Model):
    position: int
    run_id: str
    status: str
    started_at: AwareDatetime
    ended_at: AwareDatetime | None
    turn: HistoryTurn | None = None
    skip_reason: str | None = None
    payload_bytes: int = 0
    message_count: int = 0


class Batch(Model):
    stream_id: str
    after: int
    revision: int
    records: list[Source]
    previous: Episode | None = None


class Thread(Model):
    id: str
    title: str = Field(min_length=1, max_length=120)
    current_state: str = Field(min_length=1, max_length=2000)
    last_active_at: AwareDatetime
    status: Literal["active", "paused", "closed"] = "active"
    memory_ref: str | None = None
    revision: int = Field(default=1, ge=1)
    dirty: bool = False
    source_revision: int = Field(default=0, ge=0)


class ThreadRepair(Model):
    current_state: str = Field(min_length=1, max_length=2000)
    status: Literal["active", "paused", "closed"] = "active"


class RepairPage(Model):
    thread: Thread
    revision: int
    after_time: str
    after_id: str
    working_state: str
    source_count: int
    episodes: list[Episode]
    has_more: bool


class ThreadGroup(Model):
    episode_ids: list[str] = Field(min_length=1, max_length=16)
    # Existing supplied ID, or null to create. Title/state required for both.
    thread_id: str | None = None
    title: str = Field(min_length=1, max_length=120)
    current_state: str = Field(min_length=1, max_length=2000)
    status: Literal["active", "paused", "closed"] = "active"


class ThreadDecision(Model):
    groups: list[ThreadGroup] = Field(default_factory=list, max_length=16)
    no_thread: list[str] = Field(default_factory=list, max_length=16)


def gap_kind(previous: datetime, current: datetime, policy: OrganizationPolicy) -> str:
    """Classify adjacent user/Run start times; execution duration is not input proximity."""
    gap = (current - previous).total_seconds()
    if gap < 0:
        raise OrganizationError("nonmonotonic_time")
    if gap > policy.split_gap_seconds:
        return "required"
    if gap >= policy.strong_gap_seconds:
        return "strong"
    if gap >= policy.weak_gap_seconds:
        return "weak"
    return "continuous"
