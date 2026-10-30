"""One local duplex socket, explicit identity check, correlated replies, no POST retry."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from personal_agent.channels.napcat.config import NapCatSettings
from personal_agent.channels.napcat.events import numeric_id
from personal_agent.channels.qq.config import QQError, QQSettings

wire_logger = logging.Logger("napcat_wire_disabled", level=logging.CRITICAL + 1)
wire_logger.propagate = False
wire_logger.addHandler(logging.NullHandler())
logger = logging.getLogger(__name__)


class LocalConnect(connect):
    def process_redirect(self, exc: Exception) -> Exception:
        # A local service must never redirect this authenticated connection elsewhere.
        return exc


def decode(raw: str | bytes) -> dict:
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise QQError("napcat_invalid_frame") from None


def result(payload: dict, *, sending: bool = False) -> dict:
    if payload.get("status") != "ok" or type(payload.get("retcode")) is not int:
        raise QQError("napcat_action_failed", uncertain=sending)
    data = payload.get("data")
    if payload["retcode"] != 0 or not isinstance(data, dict):
        raise QQError("napcat_action_failed", uncertain=sending)
    return data


class NapCatTransport:
    def __init__(self, account: NapCatSettings, policy: QQSettings) -> None:
        account.require_napcat()
        self.account = account
        self.policy = policy
        self.state = "not_started"
        self.socket: ClientConnection | None = None
        self.pending: dict[str, asyncio.Future] = {}

    async def run(
        self,
        on_event: Callable[[dict], Awaitable[None]],
        on_ready: Callable[[], None],
    ) -> None:
        try:
            for attempt in range(self.policy.qq_reconnect_attempts + 1):
                self.state = "connecting"
                try:
                    async with LocalConnect(
                        self.account.napcat_ws_url,
                        additional_headers={
                            "Authorization": "Bearer "
                            + self.account.napcat_token.get_secret_value()
                        },
                        proxy=None,
                        open_timeout=self.policy.qq_connect_timeout_seconds,
                        close_timeout=3,
                        ping_interval=20,
                        ping_timeout=20,
                        max_size=262144,
                        max_queue=4,
                        logger=wire_logger,
                    ) as socket:
                        await self.serve(socket, on_event, on_ready)
                except QQError as exc:
                    if exc.fatal:
                        raise
                except InvalidStatus as exc:
                    if exc.response.status_code in {401, 403}:
                        raise QQError("napcat_authorization", fatal=True) from None
                except (OSError, TimeoutError, ConnectionClosed, InvalidHandshake):
                    pass
                self.state = "reconnecting"
                logger.warning("napcat_reconnecting attempt=%d", attempt)
                if attempt == self.policy.qq_reconnect_attempts:
                    raise QQError("napcat_reconnect_exhausted", fatal=True)
                await asyncio.sleep(
                    min(
                        self.policy.qq_reconnect_max_seconds,
                        self.policy.qq_reconnect_base_seconds * 2**attempt,
                    )
                )
        except asyncio.CancelledError:
            self.state = "stopped"
            raise
        except Exception:
            self.state = "error"
            raise

    async def serve(
        self,
        socket: ClientConnection,
        on_event: Callable[[dict], Awaitable[None]],
        on_ready: Callable[[], None],
    ) -> None:
        try:
            echo = uuid4().hex
            buffered = []
            async with asyncio.timeout(self.policy.qq_connect_timeout_seconds):
                await socket.send(
                    json.dumps({"action": "get_login_info", "params": {}, "echo": echo})
                )
                while True:
                    payload = decode(await socket.recv())
                    if payload.get("echo") == echo:
                        data = result(payload)
                        if numeric_id(data.get("user_id")) != self.account.napcat_self_id:
                            raise QQError("napcat_identity_mismatch", fatal=True)
                        break
                    if "post_type" in payload:
                        if len(buffered) >= self.policy.qq_queue_size:
                            raise QQError("napcat_handshake_capacity", fatal=True)
                        buffered.append(payload)
            self.socket = socket
            self.state = "ready"
            on_ready()
            for payload in buffered:
                await on_event(payload)
            while True:
                payload = decode(await socket.recv())
                echo = payload.get("echo")
                if isinstance(echo, str) and echo in self.pending:
                    future = self.pending[echo]
                    if not future.done():
                        future.set_result(payload)
                elif "post_type" in payload:
                    # The callback only validates/persists; never waits for model or sends.
                    await on_event(payload)
        finally:
            self.socket = None
            if self.state == "ready":
                self.state = "reconnecting"
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(QQError("napcat_disconnected", uncertain=True))

    async def send_text(self, openid: str, message_id: str, sequence: int, text: str) -> None:
        if (
            openid != self.account.napcat_peer_id
            or numeric_id(message_id) is None
            or type(sequence) is not int
            or not 1 <= sequence <= self.policy.qq_reply_max_parts
            or not text.strip()
            or len(text.encode("utf-8")) > self.policy.qq_text_max_bytes
        ):
            raise QQError("napcat_send_scope", fatal=True)
        socket = self.socket
        if socket is None or self.state != "ready":
            raise QQError("napcat_not_connected")
        echo = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            async with asyncio.timeout(self.policy.qq_send_timeout_seconds):
                await socket.send(
                    json.dumps(
                        {
                            "action": "send_private_msg",
                            "params": {
                                "user_id": int(self.account.napcat_peer_id),
                                # Array segments keep model-produced CQ strings literal.
                                "message": [{"type": "text", "data": {"text": text}}],
                            },
                            "echo": echo,
                        },
                        ensure_ascii=False,
                    )
                )
                data = result(await future, sending=True)
                if numeric_id(data.get("message_id")) is None:
                    raise QQError("napcat_send_receipt", uncertain=True)
        except (ConnectionClosed, OSError, TimeoutError):
            raise QQError("napcat_send_uncertain", uncertain=True) from None
        finally:
            self.pending.pop(echo, None)
            # Retrieve any disconnect error if cancellation happened during socket.send().
            if future.done() and not future.cancelled():
                future.exception()
            future.cancel()
