"""P1 §7: bounded post-answer Episode organization, preempted by console input.

The current P1 request explicitly authorizes this asyncio idle hook and input bridge.
It is not a general background task runtime: no task tools, jobs or resumed effects.
"""

import asyncio
import json
import queue
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from personal_agent.automation.service import MemoryAutomation
from personal_agent.core.history import HistoryError
from personal_agent.organization.episodes import EpisodeOrganizer
from personal_agent.organization.models import OrganizationError
from personal_agent.organization.threads import ThreadOrganizer


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


class OrganizationRuntime:
    def __init__(
        self,
        episodes: EpisodeOrganizer,
        automation: MemoryAutomation | None = None,
        threads: ThreadOrganizer | None = None,
    ) -> None:
        self.episodes = episodes
        self.automation = automation
        self.threads = threads
        self.input = AsyncInput()
        self.task: asyncio.Task | None = None
        self.last_report: dict = {}

    def schedule(self, stream_id: str, *, after_answer: bool = False) -> None:
        self.task = asyncio.create_task(self._background(stream_id, after_answer))

    async def _background(self, stream_id: str, after_answer: bool) -> None:
        try:
            if after_answer and self.automation and self.automation.policy.mode != "off":
                self.last_report = {"memory": await self.automation.run(stream_id)}
            policy = self.episodes.policy
            if not policy.enabled:
                return
            status = self.episodes.repository.status(stream_id)
            if status["state"] == "failed" or not status["last_started_at"]:
                return
            elapsed = (
                datetime.now(UTC) - datetime.fromisoformat(status["last_started_at"])
            ).total_seconds()
            if status["pending_runs_capped"] < policy.run_threshold:
                await asyncio.sleep(max(0, policy.idle_seconds - elapsed))
            self.last_report = await self.episodes.organize(stream_id, retry=False)
            if self.threads and (
                self.threads.repository.status()["pending_episodes_capped"]
                >= policy.thread_threshold
            ):
                self.last_report["thread"] = await self.threads.organize(retry=False)
            await asyncio.sleep(policy.revision_seconds)
            self.episodes.repository.finalize(stream_id, policy)
        except asyncio.CancelledError:
            raise
        except (HistoryError, OrganizationError):
            self.last_report = {"outcome": "storage_or_organization_error"}
        except Exception:
            self.last_report = {"outcome": "runtime_error"}

    async def foreground(self) -> None:
        task, self.task = self.task, None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def close(self) -> None:
        await self.foreground()
        self.input.close()

    async def command(self, stream_id: str, text: str, write: Callable[[str], None]) -> None:
        parts = text.split()
        if parts and parts[0] == "/thread":
            if self.threads is None:
                write("Thread > 当前入口未配置 Thread Organizer。")
                return
            if parts == ["/thread", "status"]:
                result = self.threads.repository.status()
            elif parts == ["/thread", "organize"]:
                result = await self.threads.organize()
            elif parts == ["/thread", "repair"]:
                result = await self.threads.repair()
            elif parts == ["/thread", "list"]:
                result = [t.model_dump(mode="json") for t in self.threads.repository.list()]
            elif len(parts) == 3 and parts[:2] == ["/thread", "show"]:
                result = self.threads.repository.get(parts[2]).model_dump(mode="json")
            else:
                write("用法：/thread status|organize|repair|list|show <id>")
                return
        elif parts == ["/episode", "status"]:
            result = {**self.episodes.repository.status(stream_id), "last_report": self.last_report}
        elif parts == ["/episode", "organize"]:
            result = await self.episodes.organize(stream_id)
        elif parts == ["/episode", "list"]:
            result = [
                episode.model_dump(mode="json")
                for episode in self.episodes.repository.list(stream_id)
            ]
        elif len(parts) == 3 and parts[:2] == ["/episode", "show"]:
            result = self.episodes.repository.get(parts[2]).model_dump(mode="json")
        else:
            write("用法：/episode status|organize|list|show <id>")
            return
        write(json.dumps(result, ensure_ascii=False))
