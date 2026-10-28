"""Single-shard official Gateway: bounded reconnect, Resume, and ACK deadlines."""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from personal_agent.channels.qq.api import QQAPI
from personal_agent.channels.qq.config import QQError, QQSettings

logger = logging.getLogger(__name__)
# A dedicated disabled logger prevents wire DEBUG logs from exposing tokens / text.
wire_logger = logging.Logger("qq_wire_disabled", level=logging.CRITICAL + 1)
wire_logger.propagate = False
wire_logger.addHandler(logging.NullHandler())

TERMINAL_CODES = {4001, 4002, 4004, 4010, 4011, 4012, 4013, 4014, 4914, 4915}
RESET_CODES = {4006, 4007, *range(4900, 4914)}


class QQGateway:
    def __init__(self, settings: QQSettings, api: QQAPI) -> None:
        self.settings = settings
        self.api = api
        self.session_id: str | None = None
        self.sequence: int | None = None
        self.state = "not_started"

    def reset(self) -> None:
        self.session_id = None
        self.sequence = None

    async def run(self, on_event: Callable[[dict], Awaitable[None]]) -> None:
        try:
            await self._run(on_event)
        except asyncio.CancelledError:
            self.state = "stopped"
            raise
        except Exception:
            self.state = "error"
            raise
        else:
            self.state = "stopped"

    async def _run(self, on_event: Callable[[dict], Awaitable[None]]) -> None:
        # Finite reconnect budget, including sessions that authenticate then drop.
        for attempt in range(self.settings.qq_reconnect_attempts + 1):
            self.state = "connecting"
            try:
                url = await self.api.gateway()
                async with connect(
                    url,
                    open_timeout=self.settings.qq_connect_timeout_seconds,
                    close_timeout=3,
                    ping_interval=None,
                    max_size=65536,
                    max_queue=4,
                    proxy=None,
                    logger=wire_logger,
                ) as socket:
                    await self.serve(socket, on_event)
            except QQError as exc:
                if exc.fatal:
                    raise
                code = exc.code
            except ConnectionClosed as exc:
                close_code = exc.rcvd.code if exc.rcvd is not None else None
                if close_code in TERMINAL_CODES:
                    raise QQError("qq_gateway_permission", fatal=True) from None
                if close_code in RESET_CODES:
                    self.reset()
                code = "qq_disconnected"
            except InvalidStatus as exc:
                if exc.response.status_code in {401, 403}:
                    raise QQError("qq_gateway_permission", fatal=True) from None
                code = "qq_handshake"
            except (OSError, TimeoutError, InvalidHandshake):
                code = "qq_connect_failed"
            logger.warning("qq_connection status=%s attempt=%d", code, attempt)
            self.state = "reconnecting"
            if attempt == self.settings.qq_reconnect_attempts:
                raise QQError("qq_reconnect_exhausted", fatal=True)
            delay = min(
                self.settings.qq_reconnect_max_seconds,
                self.settings.qq_reconnect_base_seconds * 2**attempt,
            )
            await asyncio.sleep(delay)

    async def serve(
        self, socket: ClientConnection, on_event: Callable[[dict], Awaitable[None]]
    ) -> None:
        interval: float | None = None
        next_heartbeat = float("inf")
        waiting_ack = False
        ready = False
        handshake_deadline = time.monotonic() + self.settings.qq_connect_timeout_seconds

        async def send(payload: dict) -> None:
            async with asyncio.timeout(self.settings.qq_send_timeout_seconds):
                await socket.send(json.dumps(payload, ensure_ascii=False))

        while True:
            now = time.monotonic()
            if not ready and now >= handshake_deadline:
                raise QQError("qq_ready_timeout")
            if interval is not None and now >= next_heartbeat:
                if waiting_ack:
                    raise QQError("qq_heartbeat_timeout")
                await send({"op": 1, "d": self.sequence})
                waiting_ack = True
                next_heartbeat = time.monotonic() + interval
            deadline = min(next_heartbeat, handshake_deadline if not ready else float("inf"))
            try:
                async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                    raw = await socket.recv()
            except TimeoutError:
                continue
            try:
                payload = json.loads(raw)
                if not isinstance(payload, dict) or type(payload.get("op")) is not int:
                    raise ValueError
            except (ValueError, UnicodeError, TypeError):
                raise QQError("qq_gateway_payload", fatal=True) from None
            op, data = payload["op"], payload.get("d")
            if op == 10:
                if interval is not None or not isinstance(data, dict):
                    raise QQError("qq_invalid_hello", fatal=True)
                milliseconds = data.get("heartbeat_interval")
                if type(milliseconds) is not int or not 100 <= milliseconds <= 300000:
                    raise QQError("qq_invalid_heartbeat", fatal=True)
                interval = milliseconds / 1000
                next_heartbeat = time.monotonic() + interval
                token = "QQBot " + await self.api.token()
                if self.session_id is not None:
                    await send(
                        {
                            "op": 6,
                            "d": {
                                "token": token,
                                "session_id": self.session_id,
                                "seq": self.sequence,
                            },
                        }
                    )
                else:
                    await send(
                        {"op": 2, "d": {"token": token, "intents": 1 << 25, "shard": [0, 1]}}
                    )
            elif op == 11:
                waiting_ack = False
            elif op == 1:
                await send({"op": 1, "d": self.sequence})
            elif op == 7:
                raise QQError("qq_reconnect_requested")
            elif op == 9:
                self.reset()
                raise QQError("qq_invalid_session")
            elif op == 0:
                if interval is None or type(payload.get("s")) is not int:
                    raise QQError("qq_invalid_dispatch", fatal=True)
                event = payload.get("t")
                if event == "READY":
                    session_id = data.get("session_id") if isinstance(data, dict) else None
                    if not isinstance(session_id, str) or not 1 <= len(session_id) <= 256:
                        raise QQError("qq_invalid_ready", fatal=True)
                    self.session_id = session_id
                    ready = True
                    self.state = "ready"
                    logger.info("qq_gateway_ready")
                elif event == "RESUMED":
                    ready = True
                    self.state = "ready"
                    logger.info("qq_gateway_resumed")
                elif ready:
                    # Cursor advances only after durable acceptance or explicit ignore.
                    # This callback must never wait for LLM execution or reply sending.
                    await on_event(payload)
                else:
                    raise QQError("qq_dispatch_before_ready", fatal=True)
                self.sequence = payload["s"]
