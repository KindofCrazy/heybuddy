"""Shared Host termination handling for local channel services."""

import asyncio
import signal
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def stop_on_termination(stop: asyncio.Event) -> Iterator[None]:
    # asyncio.run already handles Ctrl+C; SIGTERM also needs graceful cleanup.
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
