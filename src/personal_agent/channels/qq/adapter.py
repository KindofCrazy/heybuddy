"""Durable bounded inbox and one consumer around the existing ChatService."""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Protocol

from personal_agent.channels.qq.config import QQError, QQSettings
from personal_agent.channels.qq.events import parse_text, split_reply
from personal_agent.core.models import RunResult
from personal_agent.runtime import ChatService
from personal_agent.storage.qq import QQInbox

logger = logging.getLogger(__name__)


class TextSender(Protocol):
    async def send_text(self, openid: str, message_id: str, sequence: int, text: str) -> None: ...


class QQAdapter:
    def __init__(
        self, settings: QQSettings, service: ChatService, inbox: QQInbox, sender: TextSender
    ) -> None:
        self.settings = settings
        self.service = service
        self.inbox = inbox
        self.sender = sender
        if service.sessions is None:
            raise QQError("qq_sessions_required", fatal=True)
        if (
            inbox.store is not service.sessions.store
            or inbox.agent_id != service.sessions.agent.id
            or inbox.app_id != settings.qq_app_id
            or settings.qq_allowed_openids != (inbox.openid,)
        ):
            raise QQError("qq_scope_mismatch", fatal=True)
        self.conversation_id = service.sessions.current.id
        self.wake = asyncio.Event()
        self.worker: asyncio.Task | None = None
        self.closing = False
        self._next_send = 0.0

    def start(self) -> None:
        if self.worker is not None:
            raise QQError("qq_already_started", fatal=True)
        self.inbox.recover()
        self.worker = asyncio.create_task(self._consume(), name="qq-consumer")

    async def accept(self, payload: dict) -> None:
        if self.closing:
            raise QQError("qq_stopping")
        message = parse_text(payload, self.settings)
        if message is None or message.openid not in self.settings.qq_allowed_openids:
            return
        # No await between durable receipt and notification; Gateway cursor follows.
        if self.inbox.receive(message, self.conversation_id, self.settings.qq_queue_size):
            self.wake.set()
            logger.info("qq_received")

    def _remaining(self, row: dict) -> float:
        # Both original platform and local receive time limit the reply lifetime.
        origin = min(
            datetime.fromisoformat(row["platform_at"]),
            datetime.fromisoformat(row["received_at"]),
        )
        remaining = (
            self.settings.qq_reply_window_seconds - (datetime.now(UTC) - origin).total_seconds()
        )
        deadline = row.get("deadline")
        return min(remaining, deadline - time.monotonic()) if deadline is not None else remaining

    async def _consume(self) -> None:
        result: RunResult | None = None
        while not self.closing:
            self.wake.clear()
            row = self.inbox.claim(self.conversation_id)
            if row is None:
                self.service.after_reply(result)
                result = None
                await self.wake.wait()
                continue
            try:
                result = await self._process(row)
            except asyncio.CancelledError:
                self.inbox.mark(row["sequence"], "uncertain", error_code="qq_shutdown")
                raise
            except Exception:
                # Do not print private exception bodies or continue after storage failure.
                self.inbox.mark(row["sequence"], "uncertain", error_code="qq_processing_error")
                raise QQError("qq_processing_error", fatal=True) from None

    async def _process(self, row: dict) -> RunResult | None:
        row["deadline"] = time.monotonic() + min(
            self.settings.qq_reply_window_seconds, max(0, self._remaining(row))
        )
        await self.service.foreground()
        if self._remaining(row) <= self.settings.qq_send_timeout_seconds:
            self.inbox.mark(row["sequence"], "failed", error_code="qq_reply_expired")
            return None
        result = None
        if row["content"].lstrip().startswith("/"):
            text = "QQ 暂不支持管理命令，请在本地 CLI 操作。"
        else:
            before = self._latest_run()
            try:
                async with asyncio.timeout(
                    self._remaining(row) - self.settings.qq_send_timeout_seconds
                ):
                    result = await self.service.run(row["content"], permissions=frozenset())
            except TimeoutError:
                self.inbox.mark(row["sequence"], "failed", error_code="qq_reply_expired")
                return None
            after = self._latest_run()
            self.inbox.mark(row["sequence"], "processed", run_id=after if after != before else None)
            text = (
                result.text
                if result.status == "completed"
                else "这次处理未完成，请稍后发一条新消息。已执行的操作不会自动重试。"
            )
        await self._reply(row, text)
        return result

    def _latest_run(self) -> str | None:
        return self.inbox.latest_run_id(self.conversation_id)

    async def _reply(self, row: dict, text: str) -> None:
        sent = 0
        for sequence, part in enumerate(
            split_reply(text, self.settings.qq_text_max_bytes, self.settings.qq_reply_max_parts), 1
        ):
            delay = max(0, self._next_send - time.monotonic())
            if self._remaining(row) <= delay + self.settings.qq_send_timeout_seconds:
                self.inbox.mark(
                    row["sequence"], "failed", sent_parts=sent, error_code="qq_reply_expired"
                )
                return
            if delay:
                await asyncio.sleep(delay)
            self.inbox.mark(row["sequence"], "sending", sent_parts=sent)
            try:
                async with asyncio.timeout(self.settings.qq_send_timeout_seconds):
                    await self.sender.send_text(row["openid"], row["message_id"], sequence, part)
            except (QQError, TimeoutError) as exc:
                uncertain = isinstance(exc, TimeoutError) or exc.uncertain
                self.inbox.mark(
                    row["sequence"],
                    "uncertain" if uncertain else "failed",
                    sent_parts=sent,
                    error_code="qq_send_uncertain" if uncertain else "qq_send_failed",
                )
                logger.warning("qq_reply status=%s", "uncertain" if uncertain else "failed")
                if isinstance(exc, QQError) and exc.fatal:
                    raise
                return
            sent += 1
            self._next_send = time.monotonic() + self.settings.qq_send_interval_seconds
        self.inbox.mark(row["sequence"], "replied", sent_parts=sent)
        logger.info("qq_replied parts=%d", sent)

    async def close(self) -> None:
        self.closing = True
        self.wake.set()
        if self.worker is not None:
            self.worker.cancel()
            async with asyncio.timeout(self.settings.qq_shutdown_seconds):
                await asyncio.gather(self.worker, return_exceptions=True)
