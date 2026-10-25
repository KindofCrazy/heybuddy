"""Small persistence boundary used by ContextBuilder; no database dependencies."""

from typing import Protocol

from personal_agent.core.models import Message, RunResult, TextMessage


class HistoryError(Exception):
    """Sanitized history failure; never include message content or database SQL."""


class ConversationHistory(Protocol):
    def load(self) -> list[Message]: ...

    def begin(self, user_message: TextMessage) -> str: ...

    def append(self, run_id: str, message: Message) -> None: ...

    def finish(self, run_id: str, result: RunResult) -> None: ...

    def interrupt(self, run_id: str, *, cancelled: bool = False) -> None: ...
