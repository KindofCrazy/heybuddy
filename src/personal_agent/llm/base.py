from typing import Protocol

from personal_agent.core.models import Message, ModelResponse, ToolDefinition


class ProviderError(Exception):
    """A sanitized model request or response failure."""

    def __init__(
        self, message: str, *, code: str = "request_failed", status_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class ProviderTimeout(ProviderError):
    """A model request exceeded its time limit."""


class Provider(Protocol):
    async def generate(self, messages: list[Message], tools: list[ToolDefinition]) -> ModelResponse:
        """Return plain text and/or tool calls; raise sanitized ProviderError on failure."""
        ...
