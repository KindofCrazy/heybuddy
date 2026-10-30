from typing import Literal, Protocol

from personal_agent.core.models import Message, ModelResponse, ProviderDiagnostics, ToolDefinition

ReasoningEffort = Literal["none", "minimal", "low"]


class ProviderError(Exception):
    """A sanitized model request or response failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "request_failed",
        status_code: int | None = None,
        diagnostics: ProviderDiagnostics | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.diagnostics = diagnostics


class ProviderTimeout(ProviderError):
    """A model request exceeded its time limit."""


class Provider(Protocol):
    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        max_output_tokens: int | None = None,
        reasoning_effort: ReasoningEffort | None = None,
    ) -> ModelResponse:
        """Return plain text and/or tool calls; raise sanitized ProviderError on failure."""
        ...
