"""Small character package and context limits, without a role database."""

from pydantic import Field

from personal_agent.core.models import Model


class CharacterError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Character configuration failed ({code}).")


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
