"""Small character package and context limits, without a role database."""

from typing import Literal

from pydantic import AwareDatetime, Field

from personal_agent.core.budget import ContextError
from personal_agent.core.evidence import Evidence
from personal_agent.core.models import Model


class CharacterError(ContextError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code, f"Character configuration failed ({code}).")


class CharacterPolicy(Model):
    max_file_bytes: int = Field(default=16384, ge=1024, le=65536)
    max_context_bytes: int = Field(default=24576, ge=1024, le=65536)
    contextual_bytes: int = Field(default=6144, ge=256, le=16384)
    example_count: int = Field(default=2, ge=1, le=3)
    lore_sections: int = Field(default=2, ge=0, le=3)


class Section(Model):
    title: str
    text: str


class CharacterPackage(Model):
    character_id: str
    character: str
    behavior: str
    examples: list[Section]
    lore: list[Section]
    fingerprint: str


class StatePolicy(Model):
    enabled: bool = True
    mood_seconds: float = Field(default=3600, gt=0, le=86400)
    focus_seconds: float = Field(default=10800, gt=0, le=172800)
    min_interval_seconds: float = Field(default=900, ge=0, le=86400)
    input_tokens: int = Field(default=24576, ge=4096, le=65536)
    output_tokens: int = Field(default=2048, ge=256, le=8192)
    timeout_seconds: float = Field(default=20, gt=0, le=120)
    source_bytes: int = Field(default=32768, ge=2048, le=131072)


class StateProposal(Model):
    action: Literal["update", "no_op"]
    mood: str = Field(default="neutral", min_length=1, max_length=64)
    current_focus: str | None = Field(default=None, max_length=240)
    stance: str | None = Field(default=None, max_length=240)
    reason: str = Field(default="", max_length=400)
    evidence: list[Evidence] = Field(default_factory=list, max_length=4)


class CharacterState(Model):
    character_id: str
    revision: int = Field(ge=1)
    mood: str = Field(min_length=1, max_length=64)
    current_focus: str | None = Field(default=None, max_length=240)
    stance: str | None = Field(default=None, max_length=240)
    reason: str = Field(max_length=400)
    source_run_id: str = Field(min_length=1, max_length=128)
    observed_at: AwareDatetime
    updated_at: AwareDatetime
