"""Bounded provider output and persisted human-review state, not tool arguments."""

from typing import Literal

from pydantic import Field

from personal_agent.core.models import Model


class ReflectionError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(f"Reflection stopped ({code}); inspect the saved plan before continuing.")
        self.code = code


class ReflectionPolicy(Model):
    input_tokens: int = Field(default=16384, ge=2048, le=262144)
    output_tokens: int = Field(default=4096, ge=256, le=16384)
    timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    page_runs: int = Field(default=8, ge=1, le=32)


class Evidence(Model):
    conversation_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    quote: str = Field(min_length=4, max_length=512)


class Candidate(Model):
    category: Literal["preference", "background", "project", "decision", "knowledge", "correction"]
    subject: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=1600)
    reason: str = Field(min_length=1, max_length=1000)
    confidence: Literal["confirmed", "uncertain"]
    evidence: list[Evidence] = Field(min_length=1, max_length=8)


class Extraction(Model):
    candidates: list[Candidate] = Field(max_length=8)


class Selection(Model):
    paths: list[str] = Field(max_length=6)


class Decision(Model):
    candidate_id: int = Field(ge=1, le=8)
    action: Literal["create", "update", "no_op", "reject", "review"]
    path: str = Field(default="", max_length=260)
    old_text: str = Field(default="", max_length=8192)
    new_text: str = Field(default="", max_length=16384)
    reason: str = Field(min_length=1, max_length=1000)
    relations: str = Field(default="", max_length=1000)


class Consolidation(Model):
    decisions: list[Decision] = Field(max_length=8)


class PlanItem(Model):
    id: int
    candidate: Candidate
    decision: Decision
    before_hash: str | None = None
    after_hash: str | None = None
    state: Literal[
        "pending",
        "approved",
        "applying",
        "applied",
        "rejected",
        "skipped",
        "conflict",
        "partial",
        "uncertain",
    ] = "pending"
    approved_at: str | None = None
    result_code: str = ""
    saved_hash: str | None = None
    index_issue: dict[str, str] = Field(default_factory=dict)


class SourceRun(Model):
    run_id: str
    first_sequence: int
    last_sequence: int
    source_hash: str


class ReflectionPlan(Model):
    id: str
    conversation_id: str
    after_sequence: int
    through_sequence: int
    sources: list[SourceRun]
    items: list[PlanItem]
    created_at: str
    revision: int = 1
    closed: bool = False


TERMINAL = {"applied", "rejected", "skipped"}
