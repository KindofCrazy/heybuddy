"""Durable bounded inbox and one consumer around the existing ChatService."""

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import Protocol

from personal_agent.channels.qq.completeness import HybridTurn, judge
from personal_agent.channels.qq.config import QQError, QQSettings
from personal_agent.channels.qq.events import QQText, parse_text, split_reply
from personal_agent.channels.qq.humanize import Humanizer
from personal_agent.channels.qq.images import QQImages
from personal_agent.core.models import RunResult
from personal_agent.runtime import ChatService
from personal_agent.scheduler.chat import reply as reminder_reply
from personal_agent.scheduler.chat import source_scope as reminder_scope
from personal_agent.scheduler.models import ReminderError
from personal_agent.storage.qq import QQInbox

logger = logging.getLogger(__name__)


class TextSender(Protocol):
    async def send_text(self, openid: str, message_id: str, sequence: int, text: str) -> None: ...


class QQAdapter:
    def __init__(
        self,
        settings: QQSettings,
        service: ChatService,
        inbox: QQInbox,
        sender: TextSender,
        images: QQImages | None = None,
        *,
        scope_id: str | None = None,
        humanizer: Humanizer | None = None,
    ) -> None:
        self.settings = settings
        self.service = service
        self.inbox = inbox
        self.sender = sender
        self.images = images
        self.humanizer = humanizer or Humanizer(settings)
        if service.sessions is None:
            raise QQError("qq_sessions_required", fatal=True)
        if (
            inbox.store is not service.sessions.store
            or inbox.agent_id != service.sessions.agent.id
            or inbox.app_id != (scope_id or settings.qq_app_id)
            or settings.qq_allowed_openids != (inbox.openid,)
        ):
            raise QQError("qq_scope_mismatch", fatal=True)
        if service.scheduler is not None and (
            service.scheduler.repository.store is not inbox.store
            or service.scheduler.repository.agent_id != inbox.agent_id
        ):
            raise QQError("qq_scope_mismatch", fatal=True)
        self.conversation_id = service.sessions.current.id
        self.wake = asyncio.Event()
        self.worker: asyncio.Task | None = None
        self.closing = False
        self._next_send = 0.0
        self._receipt_version = 0
        self._hybrid = HybridTurn()
        self._turn_extensions: dict[int, float] = {}

    def note_typing(self, until: float | None) -> None:
        """Host-validated incoming presence can delay only an already queued turn."""
        if self.closing:
            return
        turn = self.inbox.preview_turn(
            self.conversation_id, self.settings, extend_after=self._turn_extensions
        )
        if turn is None or (turn.messages[0]["content"] or "").lstrip().startswith("/"):
            return
        sequence = turn.messages[-1]["sequence"]
        now = time.time()
        previous = self._turn_extensions.get(sequence, 0)
        if until is None:
            if not previous:
                return
            end = min(previous, now + self.settings.qq_message_idle_seconds)
        else:
            end = max(previous, until)
        if end == previous:
            return
        self._turn_extensions[sequence] = end
        self._hybrid.ready = ()
        self._receipt_version += 1
        self.wake.set()

    def start(self) -> None:
        if self.worker is not None:
            raise QQError("qq_already_started", fatal=True)
        self.inbox.recover()
        self.worker = asyncio.create_task(self._consume(), name="qq-consumer")

    async def accept(self, payload: dict) -> None:
        await self.accept_message(parse_text(payload, self.settings))

    async def accept_message(self, message: QQText | None) -> None:
        """Accept a channel-validated projection; never reinterpret raw wire data."""
        if self.closing:
            raise QQError("qq_stopping")
        if message is None or message.openid not in self.settings.qq_allowed_openids:
            return
        # No await between durable receipt and notification; Gateway cursor follows.
        if self.inbox.receive(message, self.conversation_id, self.settings.qq_queue_size):
            self.service.note_input()
            self._receipt_version += 1
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
            # Preempt maintenance while receipts are still safely recoverable.
            if self.inbox.pending():
                await self.service.foreground()
            turn = (
                self.inbox.preview_turn(
                    self.conversation_id, self.settings, extend_after=self._turn_extensions
                )
                if self._turn_extensions
                else None
            )
            if (
                turn
                and turn.deadline > time.time()
                and self._turn_extensions.get(turn.messages[-1]["sequence"], 0) > time.time()
            ):
                await self._wait_for_input(turn.deadline - time.time())
                continue
            if self.settings.qq_message_mode == "hybrid":
                delay = await self._check_turn()
                if delay is None:
                    continue  # Receipt snapshot changed while the judge was awaiting.
                if delay > 0:
                    await self._wait_for_input(delay)
                    continue
            row, delay = self.inbox.claim_turn(
                self.conversation_id,
                self.settings,
                hold_after=self._hybrid.hold_after,
                ready=self._hybrid.ready,
                extend_after=self._turn_extensions,
            )
            if delay > 0:
                await self._wait_for_input(delay)
                continue
            if row is None:
                self.service.after_reply(result)
                result = None
                await self.wake.wait()
                continue
            try:
                for sequence in row.get("sequences", (row["sequence"],)):
                    self._turn_extensions.pop(sequence, None)
                result = await self._process(row)
            except asyncio.CancelledError:
                if not row.get("execution_started"):
                    self.inbox.release(row["receipts"])
                    raise
                self.inbox.mark(
                    row.get("sequences", row["sequence"]),
                    "uncertain",
                    sent_parts=row.get("sent_parts", 0),
                    error_code="qq_shutdown",
                )
                raise
            except Exception:
                # Do not print private exception bodies or continue after storage failure.
                self.inbox.mark(
                    row.get("sequences", row["sequence"]),
                    "uncertain",
                    sent_parts=row.get("sent_parts", 0),
                    error_code="qq_processing_error",
                )
                raise QQError("qq_processing_error", fatal=True) from None

    async def _wait_for_input(self, delay: float) -> None:
        try:
            async with asyncio.timeout(delay):
                await self.wake.wait()
        except TimeoutError:
            pass

    async def _check_turn(self) -> float | None:
        state = self._hybrid
        turn = self.inbox.preview_turn(
            self.conversation_id,
            self.settings,
            hold_after=state.hold_after,
            extend_after=self._turn_extensions,
        )
        if turn is None:
            return 0
        state.reset_for(turn)
        now = datetime.now(UTC).timestamp()
        sequences = tuple(row["sequence"] for row in turn.messages)
        # Commands, hard boundaries, images and over-budget input retain time-only behavior.
        if (
            turn.deadline <= now
            or (turn.messages[0]["content"] or "").lstrip().startswith("/")
            or any(json.loads(row["attachments"]) for row in turn.messages)
            or sequences in state.attempted
            or len(state.attempted) >= self.settings.qq_message_judge_max_calls
            or state.elapsed >= self.settings.qq_message_judge_budget_seconds
        ):
            return 0
        delay = state.last_received(turn) + self.settings.qq_message_judge_idle_seconds - now
        if delay > 0:
            return min(delay, turn.deadline - now)
        timeout = min(
            self.settings.qq_message_judge_budget_seconds - state.elapsed,
            turn.deadline - now,
            self._remaining(turn.messages[-1]) - self.settings.qq_send_timeout_seconds,
        )
        if timeout <= 0:
            return 0
        version = self._receipt_version
        state.attempted.add(sequences)
        started = time.monotonic()
        effort = self.settings.qq_message_judge_reasoning_effort
        decision = await judge(
            self.service.agent.provider,
            turn,
            timeout,
            reasoning_effort=None if effort == "default" else effort,
        )
        state.elapsed += time.monotonic() - started
        if version != self._receipt_version:
            return None
        if decision == "ready":
            state.ready = sequences
        elif decision == "wait":
            state.hold_after = state.hold_after | {sequences[-1]}
        return 0

    async def _process(self, row: dict) -> RunResult | None:
        started = time.monotonic()
        row["deadline"] = time.monotonic() + min(
            self.settings.qq_reply_window_seconds, max(0, self._remaining(row))
        )
        await self.service.foreground()
        if self._remaining(row) <= self.settings.qq_send_timeout_seconds:
            self.inbox.mark(
                row.get("sequences", row["sequence"]), "failed", error_code="qq_reply_expired"
            )
            return None
        result = None
        if row["content"].lstrip().split(maxsplit=1)[:1] == ["/remind"]:
            # Commit a reminder before the passive acknowledgement; never replay on send failure.
            row["execution_started"] = True
            if self.service.scheduler is None:
                text = "当前入口尚未配置提醒。"
            else:
                try:
                    text = await reminder_reply(
                        self.service.scheduler,
                        self.conversation_id,
                        row["content"],
                        scope=reminder_scope(self.inbox.scope),
                        message_id=row["message_id"],
                        received_at=datetime.fromisoformat(row["received_at"]),
                        has_attachments=bool(json.loads(row.get("attachments", "[]"))),
                    )
                except ReminderError as error:
                    text = {
                        "reminder_not_found": "当前聊天中没有找到这条提醒。",
                        "reminder_time_out_of_range": "提醒时间需在未来一年内，请重新指定。",
                        "reminder_sensitive_content": "提醒内容可能包含凭据，请移除后重发。",
                        "scheduler_unavailable": "提醒服务当前不可用，尚未确认设置成功。",
                    }.get(
                        error.code,
                        "提醒参数无效，请使用 /remind in 10m 喝水，或带时区的 /remind at。",
                    )
            self.inbox.mark(row.get("sequences", row["sequence"]), "processed")
        elif row["content"].lstrip().startswith("/"):
            text = "QQ 暂不支持管理命令，请在本地 CLI 操作。"
        else:
            before = self._latest_run()
            try:
                async with asyncio.timeout(
                    self._remaining(row) - self.settings.qq_send_timeout_seconds
                ):
                    attachments = json.loads(row.get("attachments", "[]"))
                    refs = []
                    if attachments:
                        if self.images is None:
                            raise QQError("qq_image_unsupported")
                        refs = await self.images.prepare(attachments)
                    row["execution_started"] = True
                    if refs:
                        result = await self.service.run(
                            row["content"], permissions=frozenset(), images=refs
                        )
                    else:
                        result = await self.service.run(row["content"], permissions=frozenset())
            except QQError:
                await self._reply(
                    row,
                    "这次附件没有成功读取。支持图片和图片表情包（动图仅看首帧）；请换一张较小的图片重发。",
                    elapsed=time.monotonic() - started,
                )
                return None
            except TimeoutError:
                self.inbox.mark(
                    row.get("sequences", row["sequence"]), "failed", error_code="qq_reply_expired"
                )
                return None
            finally:
                after = self._latest_run()
                if after != before:
                    self.inbox.link_run(row.get("sequences", row["sequence"]), after)
            self.inbox.mark(row.get("sequences", row["sequence"]), "processed")
            if result.status == "completed":
                text = result.text
            elif result.status == "compaction_error":
                text = (
                    "历史对话整理失败，这次处理未完成。原始记录已保留；已执行的操作不会自动重试。"
                )
            else:
                text = "这次处理未完成，请稍后发一条新消息。已执行的操作不会自动重试。"
        await self._reply(
            row,
            text,
            elapsed=time.monotonic() - started,
            humanize=result is None or result.status == "completed",
        )
        return result

    def _latest_run(self) -> str | None:
        return self.inbox.latest_run_id(self.conversation_id)

    async def _reply(
        self, row: dict, text: str, *, elapsed: float = 0, humanize: bool = True
    ) -> None:
        row["execution_started"] = True
        sent = 0
        parts = (
            self.humanizer.split(text)
            if humanize
            else split_reply(
                text, self.settings.qq_text_max_bytes, self.settings.qq_reply_max_parts
            )
        )
        for sequence, part in enumerate(parts, 1):
            delay = max(0, self._next_send - time.monotonic())
            pause = (
                self.humanizer.delay(part, first=sequence == 1, elapsed=elapsed) if humanize else 0
            )
            # Cosmetic pauses overlap rate limiting, and never consume the last send window.
            if self._remaining(row) > max(delay, pause) + self.settings.qq_send_timeout_seconds:
                delay = max(delay, pause)
            if self._remaining(row) <= delay + self.settings.qq_send_timeout_seconds:
                self.inbox.mark(
                    row.get("sequences", row["sequence"]),
                    "failed",
                    sent_parts=sent,
                    error_code="qq_reply_expired",
                )
                return
            if delay:
                await asyncio.sleep(delay)
            # Scheduling or wall-clock movement may have consumed the window during sleep.
            if self._remaining(row) <= self.settings.qq_send_timeout_seconds:
                self.inbox.mark(
                    row.get("sequences", row["sequence"]),
                    "failed",
                    sent_parts=sent,
                    error_code="qq_reply_expired",
                )
                return
            self.inbox.mark(row.get("sequences", row["sequence"]), "sending", sent_parts=sent)
            try:
                async with asyncio.timeout(self.settings.qq_send_timeout_seconds):
                    await self.sender.send_text(row["openid"], row["message_id"], sequence, part)
            except (QQError, TimeoutError) as exc:
                uncertain = isinstance(exc, TimeoutError) or exc.uncertain
                self.inbox.mark(
                    row.get("sequences", row["sequence"]),
                    "uncertain" if uncertain else "failed",
                    sent_parts=sent,
                    error_code="qq_send_uncertain" if uncertain else "qq_send_failed",
                )
                logger.warning("qq_reply status=%s", "uncertain" if uncertain else "failed")
                if isinstance(exc, QQError) and exc.fatal:
                    raise
                return
            sent += 1
            row["sent_parts"] = sent
            self.inbox.mark(row.get("sequences", row["sequence"]), "processed", sent_parts=sent)
            self._next_send = time.monotonic() + self.settings.qq_send_interval_seconds
        self.inbox.mark(row.get("sequences", row["sequence"]), "replied", sent_parts=sent)
        logger.info("qq_replied parts=%d", sent)

    async def close(self) -> None:
        self.closing = True
        self.wake.set()
        if self.worker is not None:
            self.worker.cancel()
            async with asyncio.timeout(self.settings.qq_shutdown_seconds):
                await asyncio.gather(self.worker, return_exceptions=True)
