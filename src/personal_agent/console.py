"""CLI-owned cancellable bridge to blocking terminal input."""

import asyncio
import queue
import threading
from collections.abc import Callable


class AsyncInput:
    """Only terminal input runs on a daemon thread; SQLite stays on the event loop.

    A pending console read cannot be portably cancelled on Windows. A daemon avoids
    default-executor shutdown waiting forever for input after Ctrl+C.
    """

    def __init__(self) -> None:
        self.requests: queue.Queue = queue.Queue()
        self.worker = threading.Thread(target=self._worker, daemon=True, name="heybuddy-input")
        self.worker.start()

    def _worker(self) -> None:
        while True:
            request = self.requests.get()
            if request is None:
                return
            loop, future, read, prompt = request
            try:
                value, error = read(prompt), None
            except BaseException as exc:
                value, error = None, exc

            def deliver(value=value, error=error, future=future):
                if not future.done():
                    if error:
                        future.set_exception(error)
                    else:
                        future.set_result(value)

            try:
                loop.call_soon_threadsafe(deliver)
            except RuntimeError:
                return

    async def read(self, read: Callable[[str], str], prompt: str) -> str:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.requests.put((loop, future, read, prompt))
        return await future

    def close(self) -> None:
        self.requests.put(None)
