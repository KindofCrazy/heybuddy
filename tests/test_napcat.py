import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError
from websockets.asyncio.server import serve

from personal_agent import main
from personal_agent.channels.napcat import runner
from personal_agent.channels.napcat.config import NapCatSettings
from personal_agent.channels.napcat.events import parse_message
from personal_agent.channels.napcat.transport import NapCatTransport
from personal_agent.channels.qq.adapter import QQAdapter
from personal_agent.channels.qq.config import QQError
from personal_agent.channels.qq.images import QQImages
from personal_agent.config import Settings, load_settings
from personal_agent.core.models import ModelResponse
from personal_agent.runtime import assemble_chat
from personal_agent.storage.images import SQLiteImages
from personal_agent.storage.qq import QQInbox
from tests.fakes import FakeProvider
from tests.test_learning import state
from tests.test_qq_adapter import Sender, rows, settled
from tests.test_qq_images import URL, picture
from tests.test_qq_transport import config

__all__ = ["state"]


def account(**kwargs):
    return NapCatSettings(
        **{
            "napcat_enabled": True,
            "napcat_self_id": "1234567",
            "napcat_peer_id": "7654321",
            "napcat_token": "synthetic-token",
            **kwargs,
        }
    )


def event(message_id=1, text="你好", **kwargs):
    return {
        "time": int(datetime.now(UTC).timestamp()),
        "self_id": 1234567,
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": message_id,
        "user_id": 7654321,
        "sender": {"user_id": 7654321, "nickname": "never-inject-this"},
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": "never-inject-raw-cq",
        **kwargs,
    }


def settings(**kwargs):
    return Settings(
        **{
            "api_key": "synthetic",
            "model": "synthetic",
            "context_window": 64000,
            "character_id": "luna",
            "memory_learning_enabled": False,
            "memory_automation": "off",
            "organization_enabled": False,
            "proactive_enabled": False,
            "qq_message_idle_seconds": 0.001,
            "qq_humanize_enabled": False,
            "napcat_humanize_enabled": False,
            "qq_reconnect_attempts": 0,
            **account().model_dump(),
            **kwargs,
        }
    )


@pytest.mark.parametrize(
    "url",
    [
        "ws://example.com:3001",
        "ws://localhost:3001",
        "ws://127.0.0.2:3001",
        "ws://127.0.0.1",
        "ws://127.0.0.1:80",
        "wss://127.0.0.1:3001",
        "ws://user:password@127.0.0.1:3001",
        "ws://127.0.0.1:3001/?access_token=x",
        "ws://127.0.0.1:3001/#x",
        "ws://127.0.0.1:3001/api",
        " ws://127.0.0.1:3001",
    ],
)
def test_config_rejects_remote_ambiguous_or_credential_urls(url):
    with pytest.raises(ValidationError):
        account(napcat_ws_url=url)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"napcat_enabled": False},
        {"napcat_self_id": ""},
        {"napcat_peer_id": ""},
        {"napcat_peer_id": "1234567"},
        {"napcat_token": ""},
        {"napcat_token": "x\ny"},
    ],
)
def test_config_requires_distinct_accounts_and_header_safe_secret(kwargs):
    with pytest.raises(QQError, match="napcat_configuration"):
        account(**kwargs).require_napcat()


def test_config_environment_loads_without_official_credentials(monkeypatch):
    for name, value in {
        "OPENAI_API_KEY": "synthetic",
        "OPENAI_MODEL": "synthetic",
        "AGENT_CONTEXT_WINDOW": "64000",
        "AGENT_NAPCAT_ENABLED": "true",
        "AGENT_NAPCAT_SELF_ID": "1234567",
        "AGENT_NAPCAT_PEER_ID": "7654321",
        "AGENT_NAPCAT_TOKEN": "synthetic-private-token",
    }.items():
        monkeypatch.setenv(name, value)
    loaded = load_settings()
    loaded.require_napcat()
    assert not loaded.qq_enabled and not loaded.qq_app_id
    assert "synthetic-private-token" not in repr(loaded)
    assert loaded.delivery_settings(loaded).qq_allowed_openids == ("7654321",)
    assert account(napcat_ws_url="ws://[::1]:3001").napcat_ws_url == "ws://[::1]:3001"


@pytest.mark.parametrize(
    "overrides",
    [
        {"post_type": "message_sent"},
        {"message_type": "group"},
        {"sub_type": "group"},
        {"user_id": 1111111},
        {"self_id": 1111111},
        {"sender": {"user_id": 1111111}},
        {"user_id": True},
        {"message_id": False},
        {"message_id": "[CQ:evil]"},
        {"message": "[CQ:image,file=private]"},
        {"message": []},
        {"message": [{"type": [], "data": {}}]},
        {"message": [{"type": "file", "data": {"file": "secret"}}]},
        {"message": [{"type": "text", "data": {"text": 1}}]},
        {"time": "123"},
        {"time": 10**100},
        {"time": 10**12},
    ],
)
def test_projection_rejects_other_sources_and_invalid_inputs(overrides):
    assert parse_message(event(**overrides), account(), config()) is None


def test_projection_only_exposes_explicit_text_and_image_url():
    projected = parse_message(
        event(
            -123,
            message=[
                {"type": "reply", "data": {"id": "999"}},
                {"type": "text", "data": {"text": "看这个"}},
                {"type": "image", "data": {"url": URL, "file": "C:/private"}},
            ],
        ),
        account(),
        config(),
    )
    assert projected.text == "看这个" and projected.message_id == "-123"
    assert projected.attachments[0].url == URL
    assert "private" not in projected.text and "never-inject" not in str(projected)
    assert parse_message(event(text="\ud800"), account(), config()) is None
    assert parse_message(event(text="中" * 30000), account(), config()) is None


class Socket:
    def __init__(self, identity=1234567, reply="ok"):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.identity = identity
        self.reply = reply
        self.sending = asyncio.Event()

    async def send(self, raw):
        request = json.loads(raw)
        self.sent.append(request)
        if request["action"] == "get_login_info":
            # A legitimate event may arrive before the identity response.
            await self.incoming.put(json.dumps(event()))
            data = {"user_id": self.identity}
        else:
            self.sending.set()
            if self.reply == "timeout":
                return
            if self.reply == "disconnect":
                await self.incoming.put(OSError("private-network-error"))
                return
            data = {"message_id": 88}
        await self.incoming.put(
            json.dumps(
                {
                    "echo": "unrelated",
                    "status": "ok",
                    "retcode": 0,
                    "data": data,
                }
            )
        )
        await self.incoming.put(
            json.dumps(
                {
                    "echo": request["echo"],
                    "status": "ok" if self.reply != "failed" else "failed",
                    "retcode": 0 if self.reply != "failed" else 100,
                    "data": data,
                    "message": "private-platform-error",
                }
            )
        )

    async def recv(self):
        item = await self.incoming.get()
        if isinstance(item, Exception):
            raise item
        return item


async def start_transport(transport, socket):
    ready = asyncio.Event()
    received = []

    async def receive(payload):
        received.append(payload)

    task = asyncio.create_task(transport.serve(socket, receive, ready.set))
    await asyncio.wait_for(ready.wait(), 1)
    return task, received


async def test_transport_checks_identity_then_correlates_literal_text_and_receipt():
    transport = NapCatTransport(account(), config())
    socket = Socket()
    task, received = await start_transport(transport, socket)
    try:
        assert len(received) == 1 and transport.state == "ready"
        await transport.send_text("7654321", "-1", 1, "[CQ:at,qq=all]")
        assert socket.sent[1]["params"] == {
            "user_id": 7654321,
            "message": [{"type": "text", "data": {"text": "[CQ:at,qq=all]"}}],
        }
        assert not transport.pending
        with pytest.raises(QQError, match="napcat_send_scope"):
            await transport.send_text("1111111", "1", 1, "don't send")
        assert len(socket.sent) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert transport.socket is None


async def test_wrong_login_never_starts_consumer_or_accepts_buffered_event():
    transport = NapCatTransport(account(), config())

    async def forbidden(payload):
        pytest.fail("Identity has not been verified")

    with pytest.raises(QQError, match="napcat_identity_mismatch"):
        await transport.serve(Socket(identity=1111111), forbidden, lambda: pytest.fail("ready"))
    assert transport.socket is None


@pytest.mark.parametrize("reply", ["timeout", "disconnect", "failed"])
async def test_send_failures_are_uncertain_and_never_retried(reply):
    transport = NapCatTransport(account(), config(qq_send_timeout_seconds=0.02))
    socket = Socket()
    task, _ = await start_transport(transport, socket)
    socket.reply = reply
    try:
        with pytest.raises(QQError) as failure:
            await transport.send_text("7654321", "1", 1, "hello")
        assert failure.value.uncertain and len(socket.sent) == 2
        assert not transport.pending
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_send_cleans_echo_and_late_response_does_not_resend():
    transport = NapCatTransport(account(), config())
    socket = Socket()
    task, _ = await start_transport(transport, socket)
    socket.reply = "timeout"
    sending = asyncio.create_task(transport.send_text("7654321", "1", 1, "hello"))
    try:
        await asyncio.wait_for(socket.sending.wait(), 1)
        sending.cancel()
        await asyncio.gather(sending, return_exceptions=True)
        assert not transport.pending
        await socket.incoming.put(json.dumps({"echo": socket.sent[-1]["echo"], "data": {}}))
        await asyncio.sleep(0)
        assert len(socket.sent) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_local_real_websocket_authentication_and_bounded_disconnect():
    actions = []

    async def server(socket):
        assert socket.request.headers["Authorization"] == "Bearer synthetic-token"
        request = json.loads(await socket.recv())
        actions.append(request["action"])
        await socket.send(
            json.dumps(
                {
                    "echo": request["echo"],
                    "status": "ok",
                    "retcode": 0,
                    "data": {"user_id": 1234567},
                }
            )
        )
        await socket.send(json.dumps(event()))
        await socket.close()

    received = []

    async def accept(payload):
        received.append(payload)

    async with serve(server, "127.0.0.1", 0) as server_instance:
        port = server_instance.sockets[0].getsockname()[1]
        transport = NapCatTransport(
            account(napcat_ws_url=f"ws://127.0.0.1:{port}"), config(qq_reconnect_attempts=0)
        )
        with pytest.raises(QQError, match="napcat_reconnect_exhausted"):
            await transport.run(accept, lambda: None)
    assert len(received) == 1 and actions == ["get_login_info"]
    assert transport.state == "error"


@pytest.mark.parametrize("status", [302, 401, 403])
async def test_handshake_rejection_never_follows_redirect_or_starts_consumer(status):
    requests = []

    def reject(connection, request):
        requests.append(request.path)
        response = connection.respond(status, "synthetic rejection")
        if status == 302:
            response.headers["Location"] = "ws://external.invalid:3001/"
        return response

    async def unexpected(socket):
        pytest.fail("Rejected handshake must not establish a session")

    async def receive(payload):
        pytest.fail("Rejected handshake must not accept events")

    async with serve(unexpected, "127.0.0.1", 0, process_request=reject) as server_instance:
        port = server_instance.sockets[0].getsockname()[1]
        transport = NapCatTransport(
            account(napcat_ws_url=f"ws://127.0.0.1:{port}"), config(qq_reconnect_attempts=0)
        )
        expected = "napcat_authorization" if status != 302 else "napcat_reconnect_exhausted"
        with pytest.raises(QQError, match=expected):
            await transport.run(receive, lambda: pytest.fail("unauthenticated ready"))
    assert requests == ["/"] and transport.socket is None


async def test_handshake_timeout_and_malformed_frame_are_bounded():
    class Silent(Socket):
        async def send(self, raw):
            pass

    transport = NapCatTransport(account(), config(qq_connect_timeout_seconds=0.02))

    async def receive(payload):
        pytest.fail("Incomplete handshake must not accept events")

    with pytest.raises(TimeoutError):
        await transport.serve(Silent(), receive, lambda: pytest.fail("ready"))
    bad = Silent()
    await bad.incoming.put("private-non-json-frame")
    with pytest.raises(QQError, match="^napcat_invalid_frame$"):
        await transport.serve(bad, receive, lambda: pytest.fail("ready"))


async def test_receipt_namespaces_preserve_official_and_other_account_queues(state):
    db, sessions, _, _ = state
    selected = account()
    policy = selected.delivery_settings(config())
    incoming = parse_message(event(), selected, policy)
    assert incoming is not None
    scopes = ["12345", selected.napcat_scope, account(napcat_self_id="2222222").napcat_scope]
    inboxes = [QQInbox(db, sessions.agent.id, scope, selected.napcat_peer_id) for scope in scopes]
    for inbox in inboxes:
        assert inbox.receive(incoming, sessions.current.id, 1)
        assert not inbox.receive(incoming, sessions.current.id, 1)
    claimed = inboxes[1].claim(sessions.current.id)
    assert claimed["message_id"] == "1"
    inboxes[1].recover()
    assert [row["status"] for row in rows(db)] == ["received", "uncertain", "received"]
    assert inboxes[1].claim(sessions.current.id) is None
    assert inboxes[0].pending() and inboxes[2].pending()


async def test_luna_shared_runtime_images_dedup_commands_and_restart(state):
    db, sessions, memory, _ = state
    selected = settings()
    provider = FakeProvider(ModelResponse(text="我在呀。"))
    service = assemble_chat(selected, sessions, memory, provider)
    inbox = QQInbox(db, sessions.agent.id, selected.napcat_scope, selected.napcat_peer_id)
    sender = Sender()
    calls = []

    def download(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=picture())

    policy = selected.delivery_settings(selected)
    async with httpx.AsyncClient(transport=httpx.MockTransport(download)) as client:
        adapter = QQAdapter(
            policy,
            service,
            inbox,
            sender,
            QQImages(policy, client, SQLiteImages(db, sessions.agent.id)),
            scope_id=selected.napcat_scope,
        )
        adapter.start()
        try:
            incoming = event(
                message=[
                    {"type": "text", "data": {"text": "看看这张图"}},
                    {"type": "image", "data": {"url": URL, "file": "never-read-this"}},
                ]
            )
            await adapter.accept_message(parse_message(incoming, selected, policy))
            await settled(adapter)
            assert calls == [URL] and len(provider.requests) == 1
            assert sender.messages[0][-1] == "我在呀。"
            assert provider.requests[0][-1].images
            assert any("你是 Luna" in m.text for m in provider.requests[0] if m.role == "system")
            assert "never-inject" not in str(provider.requests)
            assert not {"memory_update", "memory_edit", "deferred_create"} & {
                tool.name for tool in provider.tools[0]
            }
            await adapter.accept_message(
                parse_message(event(2, text="/memory edit"), selected, policy)
            )
            await settled(adapter)
            assert len(provider.requests) == 1 and "CLI" in sender.messages[-1][-1]
            # Official and NapCat IDs cannot collide, even for the same message number.
            assert rows(db)[0]["app_id"] == selected.napcat_scope
            assert rows(db)[0]["run_id"] == db.list_runs(sessions.current.id)[-1].id
        finally:
            await adapter.close()
            await service.close()
        restarted = QQAdapter(policy, service, inbox, sender, scope_id=selected.napcat_scope)
        restarted.start()
        try:
            await restarted.accept_message(parse_message(incoming, selected, policy))
            await settled(restarted)
            assert len(provider.requests) == 1
        finally:
            await restarted.close()


async def test_runner_supervises_full_wiring_and_closes_application(state, monkeypatch):
    db, sessions, memory, _ = state
    selected = settings()
    provider = FakeProvider(ModelResponse(text="通过小号回复。"))
    service = assemble_chat(selected, sessions, memory, provider)
    delivered = asyncio.Event()
    closed = []

    @asynccontextmanager
    async def application(settings):
        try:
            yield service
        finally:
            await service.close()
            closed.append(True)

    class Transport:
        state = "ready"

        def __init__(self, *args):
            pass

        async def run(self, receive, ready):
            ready()
            await receive(event(user_id=1111111))
            await receive(event())
            await asyncio.wait_for(delivered.wait(), 2)
            raise QQError("napcat_reconnect_exhausted", fatal=True)

        async def send_text(self, openid, message_id, sequence, text):
            assert openid == "7654321" and text == "通过小号回复。"
            delivered.set()

    monkeypatch.setattr(runner, "open_application", application)
    monkeypatch.setattr(runner, "NapCatTransport", Transport)
    with pytest.raises(QQError, match="napcat_reconnect_exhausted"):
        await runner.run_napcat(selected)
    assert closed and len(provider.requests) == 1
    assert rows(db)[0]["app_id"] == selected.napcat_scope


def test_cli_napcat_fails_before_provider_when_not_configured(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["personal-agent", "napcat"])
    monkeypatch.setattr(main, "load_settings", lambda: settings(napcat_enabled=False))
    assert main.main() == 2
    assert "napcat_configuration" in capsys.readouterr().err


def test_cli_selects_napcat_dashboard_without_official_credentials(monkeypatch):
    monkeypatch.setattr("sys.argv", ["personal-agent", "napcat", "--dashboard"])
    monkeypatch.setattr(main, "load_settings", settings)
    called = []

    async def run(selected, **kwargs):
        assert not selected.qq_app_id
        called.append(kwargs)

    monkeypatch.setattr(main, "run_napcat", run)
    assert main.main() == 0 and called == [{"dashboard": True, "dashboard_port": 8765}]
