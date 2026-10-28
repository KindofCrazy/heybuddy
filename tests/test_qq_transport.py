import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic import ValidationError
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from personal_agent.channels.qq import gateway as gateway_module
from personal_agent.channels.qq.api import API_BASE, QQAPI
from personal_agent.channels.qq.config import QQError, QQSettings
from personal_agent.channels.qq.gateway import QQGateway
from personal_agent.config import Settings, load_settings


def config(**kwargs):
    return QQSettings(
        qq_enabled=True,
        qq_app_id="12345",
        qq_app_secret="test-qq-secret",
        qq_allowed_openids=("test-openid",),
        **kwargs,
    )


def test_qq_credentials_are_optional_for_cli_and_hidden(monkeypatch):
    settings = Settings(api_key="test", model="test", context_window=64000)
    assert not settings.qq_enabled
    with pytest.raises(QQError, match="qq_configuration"):
        settings.require_qq()
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("OPENAI_MODEL", "test")
    monkeypatch.setenv("AGENT_CONTEXT_WINDOW", "64000")
    monkeypatch.setenv("AGENT_QQ_APP_SECRET", "synthetic-private-value")
    monkeypatch.setenv("AGENT_QQ_ALLOWED_OPENIDS", "test-openid")
    loaded = load_settings()
    assert loaded.qq_allowed_openids == ("test-openid",)
    assert "synthetic-private-value" not in repr(loaded)
    assert "synthetic-private-value" not in loaded.model_dump_json()


@pytest.mark.parametrize("value", ["*", "one,two", "../openid", "name with spaces"])
def test_qq_requires_single_literal_openid(value):
    with pytest.raises(ValidationError):
        QQSettings(qq_allowed_openids=value)


async def test_api_token_cache_passive_reply_and_gateway():
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/app/getAppAccessToken":
            assert json.loads(request.content) == {
                "appId": "12345",
                "clientSecret": "test-qq-secret",
            }
            return httpx.Response(200, json={"access_token": "test-token", "expires_in": "7200"})
        assert request.headers["Authorization"] == "QQBot test-token"
        if request.url.path == "/gateway":
            return httpx.Response(200, json={"url": "wss://api.bot.qq.com/websocket/"})
        assert json.loads(request.content) == {
            "content": "你好",
            "msg_type": 0,
            "msg_id": "message-1",
            "msg_seq": 1,
        }
        return httpx.Response(200, json={"id": "reply-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        api = QQAPI(config(), client)
        assert await api.gateway() == "wss://api.bot.qq.com/websocket/"
        await api.send_text("test-openid", "message-1", 1, "你好")
    assert len(requests) == 3
    assert str(requests[0].url) == API_BASE + "/app/getAppAccessToken"


async def test_send_timeout_is_uncertain_and_never_retried():
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("getAppAccessToken"):
            return httpx.Response(200, json={"access_token": "test-token", "expires_in": 7200})
        raise httpx.ReadTimeout("synthetic-private-response")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(QQError) as caught:
            await QQAPI(config(), client).send_text("test-openid", "message-1", 1, "hello")
    assert caught.value.uncertain
    assert "synthetic-private-response" not in str(caught.value)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "url", ["ws://api.bot.qq.com/", "wss://attacker.test/", "wss://api.bot.qq.com@attacker.test/"]
)
async def test_gateway_url_cannot_leak_token(url):
    def handle(request):
        return httpx.Response(
            200, json={"access_token": "test-token", "expires_in": 7200, "url": url}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(QQError, match="qq_gateway_url"):
            await QQAPI(config(), client).gateway()


class FakeAPI:
    async def token(self):
        return "test-token"

    async def gateway(self):
        return "wss://api.bot.qq.com/websocket/"


class Socket:
    def __init__(self, frames):
        self.frames = asyncio.Queue()
        for frame in frames:
            self.frames.put_nowait(frame)
        self.sent = []

    async def recv(self):
        return json.dumps(await self.frames.get())

    async def send(self, raw):
        self.sent.append(json.loads(raw))


HELLO = {"op": 10, "d": {"heartbeat_interval": 100}}
READY = {"op": 0, "s": 1, "t": "READY", "d": {"session_id": "session-test"}}


async def test_gateway_identify_dispatch_then_resume():
    gateway = QQGateway(config(), FakeAPI())
    frames = [HELLO, READY, {"op": 0, "s": 2, "t": "C2C_MESSAGE_CREATE", "d": {}}, {"op": 7}]
    socket = Socket(frames)
    accepted = []

    async def accept(payload):
        accepted.append(payload)

    with pytest.raises(QQError, match="qq_reconnect_requested"):
        await gateway.serve(socket, accept)
    assert len(accepted) == 1 and gateway.sequence == 2
    assert socket.sent[0] == {
        "op": 2,
        "d": {"token": "QQBot test-token", "intents": 1 << 25, "shard": [0, 1]},
    }
    socket = Socket([HELLO, {"op": 9, "d": False}])
    with pytest.raises(QQError, match="qq_invalid_session"):
        await gateway.serve(socket, accept)
    assert socket.sent[0] == {
        "op": 6,
        "d": {"token": "QQBot test-token", "session_id": "session-test", "seq": 2},
    }
    assert gateway.session_id is None and gateway.sequence is None


async def test_gateway_heartbeat_waits_for_ack_and_stops_on_missing_ack():
    gateway = QQGateway(config(), FakeAPI())
    socket = Socket([HELLO, READY])
    with pytest.raises(QQError, match="qq_heartbeat_timeout"):
        await gateway.serve(socket, None)
    assert socket.sent[-1] == {"op": 1, "d": 1}


async def test_gateway_ack_allows_next_heartbeat():
    gateway = QQGateway(config(), FakeAPI())
    socket = Socket([HELLO, READY])
    original = socket.send

    async def send(raw):
        await original(raw)
        if json.loads(raw)["op"] == 1:
            if len(socket.sent) == 2:
                socket.frames.put_nowait({"op": 11})
            else:
                socket.frames.put_nowait({"op": 7})

    socket.send = send
    with pytest.raises(QQError, match="qq_reconnect_requested"):
        await gateway.serve(socket, None)
    assert len(socket.sent) == 3


async def test_gateway_does_not_advance_cursor_when_inbox_rejects():
    gateway = QQGateway(config(), FakeAPI())
    socket = Socket([HELLO, READY, {"op": 0, "s": 2, "t": "C2C_MESSAGE_CREATE"}])

    async def reject(_):
        raise QQError("qq_queue_full")

    with pytest.raises(QQError, match="qq_queue_full"):
        await gateway.serve(socket, reject)
    assert gateway.sequence == 1


@pytest.mark.parametrize("close_code", [4004, 4014, 4914, 4915])
async def test_gateway_permission_denial_is_terminal(monkeypatch, close_code):
    calls = []

    @asynccontextmanager
    async def connect(*args, **kwargs):
        calls.append(args)
        raise ConnectionClosedError(Close(close_code, "synthetic-private-body"), None)
        yield

    monkeypatch.setattr(gateway_module, "connect", connect)
    with pytest.raises(QQError, match="qq_gateway_permission"):
        await QQGateway(config(), FakeAPI()).run(None)
    assert len(calls) == 1


async def test_gateway_reconnect_budget_and_exponential_cap(monkeypatch):
    delays = []

    @asynccontextmanager
    async def connect(*args, **kwargs):
        raise OSError("synthetic-private-diagnostic")
        yield

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(gateway_module, "connect", connect)
    monkeypatch.setattr(gateway_module.asyncio, "sleep", sleep)
    with pytest.raises(QQError, match="qq_reconnect_exhausted"):
        await QQGateway(config(qq_reconnect_attempts=3, qq_reconnect_max_seconds=8), FakeAPI()).run(
            None
        )
    assert delays == [5, 8, 8]


async def test_token_business_rejection_stops_without_retry_or_private_logging(caplog):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"code": 4001, "message": "synthetic-private-body"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(QQError, match="qq_token_rejected") as caught:
            await QQGateway(config(), QQAPI(config(), client)).run(None)
    assert caught.value.fatal and len(calls) == 1
    assert "synthetic-private-body" not in caplog.text


async def test_total_http_timeout_bounds_trickling_response():
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"{"
            await asyncio.Event().wait()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Stream()))
    ) as client:
        api = QQAPI(config(qq_send_timeout_seconds=0.01), client)
        async with asyncio.timeout(1):
            with pytest.raises(QQError, match="qq_network_error"):
                await api.token()


async def test_passive_sender_rejects_unauthorized_target_without_network():
    def forbidden(request):
        pytest.fail("Unauthorized reply must not reach the network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(QQError, match="qq_invalid_reply"):
            await QQAPI(config(), client).send_text("stranger", "message-1", 1, "hello")
