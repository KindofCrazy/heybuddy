"""Conservative input estimates and explicit, model-independent host budgets."""

import json

from pydantic import Field, model_validator

from personal_agent.core.models import Message, Model, ToolDefinition


class ContextError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ContextBudget(Model):
    context_window: int = Field(ge=4096, le=4_000_000)
    reserved_output_tokens: int = Field(default=4096, ge=16)
    safety_margin: int = Field(default=1024, ge=0)
    trigger_ratio: float = Field(default=0.8, gt=0, lt=1)
    recent_runs: int = Field(default=2, ge=1, le=16)
    page_runs: int = Field(default=32, ge=2, le=64)
    summary_tokens: int = Field(default=2048, ge=64, le=16384)
    summary_output_tokens: int = Field(default=4096, ge=16)
    compaction_input_tokens: int = Field(default=16384, ge=2048)
    compaction_timeout: float = Field(default=30, gt=0, allow_inf_nan=False)
    max_compaction_steps: int = Field(default=16, ge=1, le=128)

    @property
    def input_limit(self) -> int:
        return self.context_window - self.reserved_output_tokens - self.safety_margin

    @property
    def summary_input_limit(self) -> int:
        return min(
            self.compaction_input_tokens,
            self.context_window - self.summary_output_tokens - self.safety_margin,
        )

    @property
    def load_bytes(self) -> int:
        return min(2_097_152, max(self.input_limit, self.summary_input_limit))

    @model_validator(mode="after")
    def coherent(self):
        if (
            self.input_limit < 1024
            or self.summary_input_limit < self.summary_tokens + 1024
            or self.summary_tokens > self.summary_output_tokens
            or self.page_runs <= self.recent_runs
        ):
            raise ValueError("Inconsistent context/output/summary/recent budgets")
        return self


def estimate_value(value: object) -> int:
    # One serialized UTF-8 byte per estimated token, plus framing. Deliberately
    # overcounts duplicated internal/wire fields; not an exact tokenizer or guarantee.
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def estimate_request(messages: list[Message], tools: list[ToolDefinition]) -> int:
    return (
        256
        + sum(estimate_value(m.model_dump()) + 16 for m in messages)
        + estimate_value([tool.model_dump() for tool in tools])
    )
