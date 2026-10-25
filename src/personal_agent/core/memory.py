"""Core Memory compilation boundary; no filesystem implementation in the Runtime."""

from typing import Protocol


class CoreMemoryError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CoreMemory(Protocol):
    def compile(self) -> str:
        """Return a size-limited, data-only context fragment or fail closed."""
        ...
