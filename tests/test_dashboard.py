import httpx
import pytest

from personal_agent.config import Settings
from personal_agent.core.history import HistoryError
from personal_agent.dashboard.app import create_app
from personal_agent.memory.store import MemoryFS
from personal_agent.runtime import assemble_chat
from personal_agent.sessions import SessionManager
from personal_agent.storage.sqlite import SQLiteStore
from tests.fakes import FakeProvider
from tests.test_episodes import seed


@pytest.fixture
async def observatory(tmp_path):
    with SQLiteStore(tmp_path / "dashboard.db") as store:
        store.initialize()
        sessions = SessionManager(store)
        memory = MemoryFS.for_agent(tmp_path / "memory", sessions.agent.id)
        memory.initialize()
        sessions.memory = memory
        settings = Settings(api_key="synthetic", model="synthetic", context_window=131072)
        provider = FakeProvider()
        service = assemble_chat(settings, sessions, memory, provider)
        app = create_app(service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as client:
            yield client, store, service, provider, app
        await service.close()


async def test_foundation_scoped_pagination_and_no_model_calls(observatory):
    client, store, service, provider, app = observatory
    stream = service.sessions.current.id
    ids = [seed(store, stream, str(i)) for i in range(5)]
    foreign_agent = store.create_agent("Other", "prompt", agent_id="other")
    foreign_stream = store.create_conversation(foreign_agent.id)
    foreign_run = seed(store, foreign_stream.id)
    first = (await client.get("/api/runs?limit=2")).json()
    second = (await client.get(f"/api/runs?limit=2&cursor={first['next_cursor']}")).json()
    assert [r["id"] for r in first["items"] + second["items"]] == ids[::-1][:4]
    assert (await client.get(f"/api/runs/{foreign_run}")).status_code == 404
    assert (await client.get("/api/runs/missing")).status_code == 404
    assert (await client.get("/api/status")).json()["state"] == "Idle"
    assert (await client.get("/")).status_code == 200
    assert (await client.get("/api/runs?limit=100000")).status_code == 422
    assert app.state.service is service
    assert not provider.requests


async def test_foundation_boundary_and_reads_do_not_write(observatory):
    client, store, service, provider, _ = observatory
    before = store._connection.total_changes
    for route in (
        "/",
        "/api/status",
        "/api/runs",
        "/api/episodes",
        "/api/threads",
        "/api/learning/jobs",
    ):
        response = await client.get(route)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert store._connection.total_changes == before
    assert (await client.post("/api/runs", json={"text": "run"})).status_code == 405
    assert (await client.get("/api/status", headers={"host": "evil.example"})).status_code == 403
    assert (
        await client.get("/api/status", headers={"origin": "https://evil.example"})
    ).status_code == 403
    assert (
        await client.get("/api/status", headers={"sec-fetch-site": "cross-site"})
    ).status_code == 403
    with pytest.raises(HistoryError):
        SQLiteStore(store.path)
    assert not provider.requests
