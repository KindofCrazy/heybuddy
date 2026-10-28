import asyncio
import socket
from contextlib import asynccontextmanager

import httpx
import pytest

from personal_agent.channels.qq import runner
from personal_agent.channels.qq.adapter import QQAdapter
from personal_agent.channels.qq.config import QQError
from personal_agent.config import Settings
from personal_agent.core.history import HistoryError
from personal_agent.core.models import ModelResponse
from personal_agent.dashboard.app import create_app
from personal_agent.dashboard.repository import ObservatoryRepository
from personal_agent.dashboard.server import dashboard_server
from personal_agent.runtime import assemble_chat
from personal_agent.storage.qq import QQInbox
from tests.test_dashboard import observatory
from tests.test_qq_adapter import Sender, event, settled
from tests.test_qq_transport import config

__all__ = ["observatory"]


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def test_http_failure_isolated_from_actual_qq_consumer_and_shutdown(observatory, monkeypatch):
    _, store, service, provider, _ = observatory
    port = free_port()
    sender = Sender()
    adapter = QQAdapter(
        config(), service, QQInbox(store, "heybuddy", "12345", "test-openid"), sender
    )
    adapter.start()
    try:
        async with dashboard_server(service, port):
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False
            ) as client:
                assert (await client.get("/api/status")).status_code == 200
                assert not provider.requests
                with monkeypatch.context() as patch:

                    def unavailable(*args, **kwargs):
                        raise HistoryError("private exception data must never be returned")

                    patch.setattr(ObservatoryRepository, "listing", unavailable)
                    response = await client.get("/api/runs")
                    assert response.status_code == 503
                    assert "private exception" not in response.text
                    provider.responses.append(ModelResponse(text="QQ still works"))
                    await adapter.accept(event(text="synthetic message during HTTP failure"))
                    await settled(adapter)
                    assert sender.messages[-1][-1] == "QQ still works"
                    assert not adapter.worker.done()
                assert len((await client.get("/api/runs")).json()["items"]) == 1
                with pytest.raises(HistoryError):
                    from personal_agent.storage.sqlite import SQLiteStore

                    SQLiteStore(store.path)
        async with httpx.AsyncClient(trust_env=False) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(f"http://127.0.0.1:{port}/api/status")
    finally:
        await adapter.close()


async def test_qq_runner_reuses_exact_service_with_live_http_task(observatory, monkeypatch):
    _, store, service, provider, _ = observatory
    provider.responses.append(ModelResponse(text="same service"))
    port = free_port()
    received_service = []
    delivered = asyncio.Event()

    @asynccontextmanager
    async def application(settings):
        received_service.append(service)
        yield service

    class API(Sender):
        def __init__(self, *args):
            super().__init__()

        async def send_text(self, *args):
            await super().send_text(*args)
            delivered.set()

    class Gateway:
        state = "ready"

        def __init__(self, *args):
            pass

        async def run(self, accept):
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False
            ) as client:
                assert (await client.get("/api/status")).json()["qq"]["gateway"] == "ready"
                assert not provider.requests
                await accept(event(text="QQ runner synthetic input"))
                await asyncio.wait_for(delivered.wait(), 3)
                raise QQError("synthetic_stop", fatal=True)

    monkeypatch.setattr(runner, "open_application", application)
    monkeypatch.setattr(runner, "QQAPI", API)
    monkeypatch.setattr(runner, "QQGateway", Gateway)
    settings = Settings(
        api_key="synthetic", model="synthetic", context_window=64000, **config().model_dump()
    )
    with pytest.raises(QQError, match="synthetic_stop"):
        await runner.run_qq(settings, dashboard=True, dashboard_port=port)
    assert received_service == [service] and len(provider.requests) == 1
    assert store.list_runs(service.sessions.current.id)[0].status == "completed"


async def test_port_conflict_and_cli_public_binding_fail_closed(observatory, monkeypatch):
    from personal_agent import main

    _, _, service, provider, _ = observatory
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(OSError):
            async with dashboard_server(service, listener.getsockname()[1]):
                pytest.fail("must not start on an occupied port")
    for argv in (
        ["personal-agent", "web", "--host", "0.0.0.0"],
        ["personal-agent", "web", "--dashboard-port", "70000"],
    ):
        monkeypatch.setattr("sys.argv", argv)
        with pytest.raises(SystemExit) as error:
            main.main()
        assert error.value.code == 2
    assert not provider.requests


async def test_observer_assembly_does_not_recover_or_publish_memory(observatory):
    _, store, original, provider, _ = observatory
    settings = Settings(
        api_key="synthetic", model="synthetic", context_window=131072, character_id="sherlock_bbc"
    )
    # Existing constructors must not create/update schedule, learning or state rows.
    with store._transaction() as db:
        db.execute("DELETE FROM memory_learning_state")
    before = store._connection.total_changes
    observer = assemble_chat(
        settings, original.sessions, original.sessions.memory, provider, observer_only=True
    )
    try:
        assert store._connection.total_changes == before
        assert not observer.organization.character_state.repository.exists()
        with pytest.raises(RuntimeError, match="Observer-only"):
            await observer.run("never executed")
        observer.after_reply()
        assert observer.organization.task is None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(observer)),
            base_url="http://127.0.0.1:8765",
        ) as client:
            assert (await client.get("/api/character/state")).json()["active"] is None
            assert (await client.get("/api/status")).status_code == 200
        assert store._connection.total_changes == before and not provider.requests
    finally:
        await observer.close()
