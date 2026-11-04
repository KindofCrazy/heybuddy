"""Host-owned, bounded lifecycle extensions; callbacks never grant permissions."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

EventName = Literal[
    "RunStarted", "BeforeModelCall", "AfterModelCall", "PreToolUse", "PostToolUse", "Stop"
]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LifecycleEvent:
    name: EventName
    run_id: str | None = None
    round_number: int = 0
    call_id: str | None = None
    tool_name: str | None = None
    status: str | None = None


Hook = Callable[[LifecycleEvent], Awaitable[str | None]]


class HookRegistry:
    def __init__(self, timeout: float = 1, event_timeout: float = 2) -> None:
        if not 0 < timeout <= event_timeout < float("inf"):
            raise ValueError("Invalid hook deadlines")
        self.timeout, self.event_timeout = timeout, event_timeout
        self._hooks: dict[str, list[Hook]] = {}

    def register(self, event: EventName, callback: Hook) -> None:
        if event not in EventName.__args__:
            raise ValueError("Unknown lifecycle event")
        self._hooks.setdefault(event, []).append(callback)

    async def emit(self, event: LifecycleEvent) -> str | None:
        blocking = event.name == "PreToolUse"
        try:
            async with asyncio.timeout(self.event_timeout):
                for callback in self._hooks.get(event.name, ()):
                    try:
                        async with asyncio.timeout(self.timeout):
                            denied = await callback(event)
                        if blocking and denied is not None:
                            return "hook_denied"
                    except TimeoutError:
                        if blocking:
                            return "hook_timeout"
                    except Exception as exc:
                        logger.warning(
                            "hook_failed event=%s type=%s", event.name, type(exc).__name__
                        )
                        if blocking:
                            return "hook_failed"
        except TimeoutError:
            if blocking:
                return "hook_timeout"
        return None
