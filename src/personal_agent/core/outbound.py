"""Optional read-only assistant-origin notices, independent of any storage implementation."""

from typing import Protocol


class ConversationNotices(Protocol):
    def context(self) -> dict | None: ...
