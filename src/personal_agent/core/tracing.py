"""Optional local audit snapshots of main-agent requests, never replay context."""

from typing import Protocol

from personal_agent.core.models import Message, ModelResponse, ToolDefinition


class RunTrace(Protocol):
    def start(
        self,
        run_id: str,
        number: int,
        messages: list[Message],
        tools: list[ToolDefinition],
        max_output_tokens: int | None,
    ) -> None: ...

    def finish(
        self,
        run_id: str,
        number: int,
        state: str,
        elapsed_ms: int,
        response: ModelResponse | None = None,
    ) -> None: ...
