from typing import Literal

from pydantic import Field, field_validator

from personal_agent.core.models import Model

Mode = Literal["off", "suggest", "auto"]


class AutomationError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AutomationPolicy(Model):
    mode: Mode = "suggest"
    input_tokens: int = Field(default=16384, ge=2048, le=65536)
    output_tokens: int = Field(default=4096, ge=256, le=16384)
    max_requests: int = Field(default=4, ge=1, le=4)
    max_tokens: int = Field(default=131072, ge=4096, le=524288)
    timeout_seconds: float = Field(default=180, gt=0, allow_inf_nan=False)
    # Host authorizes exact content, not a model's claim of public/safe content.
    auto_create_grants: dict[str, str] = Field(default_factory=dict, max_length=16)

    @field_validator("auto_create_grants")
    @classmethod
    def valid_grants(cls, value: dict[str, str]) -> dict[str, str]:
        import re

        for path, content_hash in value.items():
            if (
                not re.fullmatch(r"knowledge/(?:[A-Za-z0-9_-]+/){0,2}[A-Za-z0-9_-]+\.md", path)
                or path.endswith("/MEMORY.md")
                or not re.fullmatch(r"[0-9a-f]{64}", content_hash)
            ):
                raise ValueError("Grants require knowledge topic paths and SHA256 content hashes")
        return value


class Worthiness(Model):
    should_reflect: bool = Field(strict=True)
    reason: str = Field(min_length=1, max_length=500)
    category: Literal[
        "preference", "background", "project", "goal", "correction", "knowledge", "none"
    ]
    confidence: Literal["high", "low"]
