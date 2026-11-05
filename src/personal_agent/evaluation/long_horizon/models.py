"""Strict benchmark contracts, separate from production prompts and journals."""

from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Turn(Schema):
    turn_id: str = Field(min_length=1)
    timestamp: AwareDatetime
    user_message: str = Field(min_length=1)
    frozen_assistant_message: str
    source: str = Field(min_length=1)
    conversation_key: str = Field(min_length=1)


class CheckpointTrigger(Schema):
    checkpoint_id: str
    after_turn_id: str


class Checkpoint(CheckpointTrigger):
    question: str
    reference_answer: str
    evidence_turn_ids: list[str]


class Scenario(Schema):
    scenario_id: str = Field(pattern=r"^[A-Z]+[0-9]+$")
    description: str
    time_zone: str
    turns: list[Turn] = Field(min_length=1)
    checkpoints: list[CheckpointTrigger]
    tags: list[str]

    @model_validator(mode="after")
    def validate_history(self):
        if self.time_zone not in {"Asia/Shanghai", "UTC"}:
            try:
                ZoneInfo(self.time_zone)
            except ZoneInfoNotFoundError:
                raise ValueError("Unsupported time zone; install system tzdata") from None
        ids = [t.turn_id for t in self.turns]
        checkpoint_ids = [c.checkpoint_id for c in self.checkpoints]
        if len(set(ids)) != len(ids) or len(set(checkpoint_ids)) != len(checkpoint_ids):
            raise ValueError("Duplicate ID")
        if any(b.timestamp < a.timestamp for a, b in zip(self.turns, self.turns[1:], strict=False)):
            raise ValueError("Turns must be chronological")
        if any(c.after_turn_id not in ids for c in self.checkpoints):
            raise ValueError("Unknown checkpoint turn")
        return self


class RoutingGold(Schema):
    turn_id: str
    gold_topic_id: str | None
    acceptable_actions: list[Literal["continue", "select", "new", "chat", "clarify"]] = Field(
        min_length=1
    )
    ambiguity: bool
    relevant_source_turn_ids: list[str]

    @model_validator(mode="after")
    def topic_required(self):
        if len(set(self.acceptable_actions)) != len(self.acceptable_actions):
            raise ValueError("Duplicate acceptable action")
        if not self.ambiguity and not self.gold_topic_id:
            raise ValueError("Unambiguous routing requires a topic")
        return self


class BenchmarkProtocol(Schema):
    version: str
    scorer_version: str
    annotation_origin: str
    human_reviewed: bool
    expected_splits: dict[str, int]
    min_turns: int = Field(ge=1)
    max_turns: int = Field(ge=1)
    scenario_notes: dict[str, list[str]]


class MemoryGold(Schema):
    fact_id: str
    key: str
    value: str
    valid_from: AwareDatetime
    valid_until: AwareDatetime | None
    evidence_turn_ids: list[str]
    memory_action: Literal["create", "update", "delete", "retain"]

    @model_validator(mode="after")
    def interval(self):
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("Invalid fact interval")
        return self


class ScenarioGold(Schema):
    scenario_id: str
    routing: list[RoutingGold]
    memory: list[MemoryGold]
    checkpoints: list[Checkpoint]

    def validate_for(self, scenario: Scenario) -> None:
        ids = [t.turn_id for t in scenario.turns]
        for rows, key in ((self.routing, "turn_id"), (self.memory, "fact_id")):
            values = [getattr(row, key) for row in rows]
            if len(values) != len(set(values)):
                raise ValueError("Duplicate gold ID")
        if self.scenario_id != scenario.scenario_id:
            raise ValueError("Gold scenario mismatch")
        if self.routing and {r.turn_id for r in self.routing} != set(ids):
            raise ValueError("Routing gold coverage mismatch")
        for row in self.routing:
            if row.turn_id not in ids or any(
                ref not in ids[: ids.index(row.turn_id) + 1] for ref in row.relevant_source_turn_ids
            ):
                raise ValueError("Invalid routing evidence")
        for row in self.memory:
            if not row.evidence_turn_ids or any(ref not in ids for ref in row.evidence_turn_ids):
                raise ValueError("Invalid memory evidence")
            if any(
                t.timestamp > row.valid_from
                for t in scenario.turns
                if t.turn_id in row.evidence_turn_ids
            ):
                raise ValueError("Fact predates evidence")
        if [
            CheckpointTrigger(checkpoint_id=c.checkpoint_id, after_turn_id=c.after_turn_id)
            for c in self.checkpoints
        ] != scenario.checkpoints:
            raise ValueError("Checkpoint gold mismatch")
        for checkpoint in self.checkpoints:
            if any(
                ref not in ids[: ids.index(checkpoint.after_turn_id) + 1]
                for ref in checkpoint.evidence_turn_ids
            ):
                raise ValueError("Invalid checkpoint evidence")


class Config(Schema):
    variant: Literal["rule"] = "rule"
    replay_mode: Literal["streaming"] = "streaming"
    seed: int = 0
    network: Literal[False] = False


class Prediction(Schema):
    scenario_id: str
    turn_id: str
    actual_thread_id: str
    previous_thread_id: str | None
    routing_reason: str
    whether_new_thread_created: bool
    ambiguity_flag: bool
    request_count: int = Field(ge=0)
    latency_ms: float = Field(ge=0, allow_inf_nan=False)
    error_code: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class Snapshot(Schema):
    checkpoint_id: str
    after_turn_id: str
    completed_turn_ids: list[str]
    bindings: dict[str, str]
    memory_blocks: dict[str, str]


class ReplayResult(Schema):
    scenario_id: str
    status: Literal["completed", "failed", "cancelled"]
    predictions: list[Prediction]
    snapshots: list[Snapshot]
    error_stage: Literal["setup", "routing", "journal", "checkpoint", "maintenance"] | None = None
    error_code: str | None = None
    maintenance: list[dict] = Field(default_factory=list)

    def validate_for(self, scenario: Scenario) -> None:
        if self.scenario_id != scenario.scenario_id or self.status != "completed":
            raise ValueError("Replay incomplete")
        if self.error_code or self.error_stage:
            raise ValueError("Replay contains errors")
        if [p.turn_id for p in self.predictions] != [t.turn_id for t in scenario.turns]:
            raise ValueError("Prediction coverage mismatch")
        if any(p.scenario_id != self.scenario_id or p.error_code for p in self.predictions):
            raise ValueError("Prediction scope/error mismatch")
        if [s.checkpoint_id for s in self.snapshots] != [
            c.checkpoint_id
            for c in sorted(
                scenario.checkpoints,
                key=lambda c: [t.turn_id for t in scenario.turns].index(c.after_turn_id),
            )
        ]:
            raise ValueError("Checkpoint coverage mismatch")
        turns = [t.turn_id for t in scenario.turns]
        triggers = {c.checkpoint_id: c for c in scenario.checkpoints}
        for snapshot in self.snapshots:
            trigger = triggers[snapshot.checkpoint_id]
            expected = turns[: turns.index(trigger.after_turn_id) + 1]
            if (
                snapshot.after_turn_id != trigger.after_turn_id
                or snapshot.completed_turn_ids != expected
                or snapshot.bindings
                != {
                    p.turn_id: p.actual_thread_id for p in self.predictions if p.turn_id in expected
                }
            ):
                raise ValueError("Checkpoint state mismatch")
