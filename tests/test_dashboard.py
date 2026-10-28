import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from personal_agent.config import Settings
from personal_agent.core.history import HistoryError
from personal_agent.core.models import AssistantMessage, TextMessage, ToolCall
from personal_agent.dashboard.app import create_app
from personal_agent.memory.store import MemoryFS
from personal_agent.runtime import assemble_chat
from personal_agent.sessions import SessionManager
from personal_agent.storage.sqlite import SQLiteStore
from tests.fakes import FakeProvider
from tests.test_episodes import organizer, seed
from tests.test_threads import thread_service


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


async def test_thread_episode_run_relationships_and_unassigned_episode(observatory):
    client, store, service, provider, app = observatory
    stream = service.sessions.current.id
    rid = seed(store, stream, "private source")
    episodes = app.state.repository.episodes
    await organizer(episodes).organize(stream)
    eid = episodes.list(stream)[0].id
    assert (await client.get(f"/api/episodes/{eid}")).json()["thread_id"] is None
    await thread_service(store).organize()
    thread = (await client.get("/api/threads")).json()["items"][0]
    linked = (await client.get(f"/api/episodes?thread_id={thread['id']}")).json()
    assert [e["id"] for e in linked["items"]] == [eid]
    assert (await client.get(f"/api/episodes/{eid}")).json()["run_ids"] == [rid]
    assert "private source" not in (await client.get(f"/api/runs/{rid}")).text
    messages = (await client.get(f"/api/runs/{rid}/messages?limit=1")).json()
    assert messages["items"][0]["text"] == "private source"
    after = messages["next_cursor"]
    assert (await client.get(f"/api/runs/{rid}/messages?after={after}")).json()["items"][0][
        "role"
    ] == "assistant"
    assert not provider.requests


async def test_private_body_redaction_escaping_and_opaque_provider_exclusion(observatory):
    client, store, service, _, _ = observatory
    rid = store.begin_run(
        service.sessions.current.id,
        TextMessage(
            role="user",
            text='<script>alert("xss")</script> sk-abcdefghijklmno AppSecret=supersecret123',
        ),
    )
    store.append_message(
        rid,
        AssistantMessage(
            text="Visible response",
            provider_data={"opaque": [{"secret": "never-expose-reasoning"}]},
            tool_calls=[
                ToolCall(id="audit-call", name="unknown_tool", arguments='{"token":"plain"}')
            ],
        ),
    )
    store.interrupt_run(rid)
    response = await client.get(f"/api/runs/{rid}/messages")
    assert response.status_code == 200
    assert "never-expose-reasoning" not in response.text
    assert "supersecret123" not in response.text and "sk-abcdefghijklmno" not in response.text
    assert response.json()["items"][1]["tool_calls"][0]["id"] == "audit-call"
    assert (
        (await client.get("/")).headers["content-security-policy"].startswith("default-src 'self'")
    )


async def test_memory_safe_tree_markdown_and_file_receipts(observatory):
    client, store, service, _, _ = observatory
    memory = service.sessions.memory
    (memory.root / "topics").mkdir()
    (memory.root / "topics" / "notes.md").write_text(
        "# Notes\n<script>alert(1)</script>\n![image](https://example.com/beacon)\n"
        "[link](javascript:alert(1))\nAPI_KEY=secret123456",
        encoding="utf-8",
    )
    (memory.root / "unsafe.txt").write_text("hidden", encoding="utf-8")
    first = (await client.get("/api/memory?limit=2")).json()
    second = (await client.get(f"/api/memory?limit=2&cursor={first['next_cursor']}")).json()
    assert len(first["items"] + second["items"]) == 4
    assert "unsafe.txt" not in str(first) + str(second)
    response = (await client.get("/api/memory/file?path=topics/notes.md")).json()
    assert "<script" not in response["html"] and "<img" not in response["html"]
    assert "<a " not in response["html"] and "secret123456" not in str(response)
    for path in ("../.env", "C:/Windows/win.ini", "topics/../../other/human.md"):
        assert (await client.get("/api/memory/file", params={"path": path})).status_code == 404
    assert (await client.get("/api/memory/file?path=human.md")).status_code == 200
    assert (await client.get("/api/memory/file?path=topics/deleted.md")).status_code == 404
    learning = service.organization.learning.repository
    rid = seed(store, service.sessions.current.id)
    jid = learning.create("priority", {"runs": [{"id": rid}], "episodes": []}, datetime.now(UTC))
    learning.save(
        jid,
        "completed",
        datetime.now(UTC),
        operations=[
            {
                "path": "topics/notes.md",
                "action": "create",
                "state": "applied",
                "hash": response["hash"],
            }
        ],
    )
    record = (await client.get(f"/api/learning/jobs/{jid}")).json()
    assert record["sources"]["runs"][0]["id"] == rid and record["state"] == "completed"
    assert (await client.get("/api/memory/records?path=topics/notes.md")).json()["items"][0][
        "id"
    ] == jid


async def test_character_disabled_is_honest_and_files_are_packaged(observatory):
    client, store, service, _, _ = observatory
    assert (await client.get("/api/character")).json()["enabled"] is False
    state = (await client.get("/api/character/state")).json()
    assert state["active"] is None and state["items"] == []
    html = (await client.get("/")).text
    assert "OBJECT INSPECTOR" in html and 'id="breadcrumbs"' in html
    for path in (
        "observatory.css",
        "components.js",
        "overview.js",
        "inspectors.js",
        "details.js",
        "memory-character.js",
    ):
        assert (await client.get(f"/static/{path}")).status_code == 200
    assert (await client.get("/static/../../../.env")).status_code == 404


async def test_character_state_uses_actual_p3_model_and_receipts(observatory):
    from personal_agent.character.context import CharacterContextCompiler
    from personal_agent.character.models import CharacterPolicy
    from tests.test_character_state import StateProvider, tagged_run
    from tests.test_character_state import service as character_service
    from tests.test_organization_schedule import FakeClock

    client, store, service, _, _ = observatory
    clock = FakeClock()
    actor = character_service(store, service.sessions.memory, StateProvider(), clock)
    service.organization.character_state = actor
    service.sessions.character = CharacterContextCompiler(actor.package, CharacterPolicy())
    rid = tagged_run(store, service.sessions.current.id, actor, clock)
    await organizer(actor.repository.episodes).organize(service.sessions.current.id)
    assert (await actor.run(force=True))["outcome"] == "completed"
    before = store._connection.total_changes
    data = (await client.get("/api/character/state")).json()
    assert data["active"] == actor.active()
    assert data["stored"]["source_run_id"] == rid and data["items"][0]["state"] == "completed"
    assert "relationship" not in data["active"]
    assert (await client.get("/api/character")).json()["character_id"] == actor.package.character_id
    assert store._connection.total_changes == before


async def test_activity_pagination_and_readonly_runtime_diagnostics(observatory):
    client, store, service, provider, _ = observatory
    ids = [seed(store, service.sessions.current.id) for _ in range(4)]
    before = store._connection.total_changes
    first = (await client.get("/api/activity?limit=3")).json()
    second = (
        await client.get(
            "/api/activity",
            params={
                "limit": 3,
                "cursor": first["next_cursor"],
            },
        )
    ).json()
    assert not ({e["event_id"] for e in first["items"]} & {e["event_id"] for e in second["items"]})
    assert all(e["related_object_id"] in ids for e in first["items"] + second["items"])
    assert all(e["origin"] == "persistent_fact" for e in first["items"])
    assert first["scope_limited"]
    assert (await client.get("/api/activity?cursor=bad-cursor")).status_code == 422
    status = (await client.get("/api/status")).json()
    assert status["diagnostics"]["schedule"] is None
    assert not status["maintenance_running"]
    assert status["diagnostics"]["pending_runs_capped"] == 4
    assert store._connection.total_changes == before
    assert not provider.requests


async def test_live_stage_is_observed_only_during_actual_execution(observatory):
    client, store, service, _, _ = observatory
    entered, release = asyncio.Event(), asyncio.Event()

    async def maintenance():
        entered.set()
        await release.wait()
        return {"outcome": "failed", "error_code": "synthetic_failure"}

    task = asyncio.create_task(service.organization._execute("curator", maintenance()))
    try:
        await entered.wait()
        status = (await client.get("/api/status")).json()
        assert status["current_stage"] == "curator" and status["maintenance_running"]
        events = (await client.get("/api/activity")).json()["items"]
        assert any(
            e["event_type"] == "curator_started" and e["origin"] == "live_runtime" for e in events
        )
        release.set()
        await task
        assert not (await client.get("/api/status")).json()["maintenance_running"]
        assert service.organization.events[-1]["event_type"] == "curator_failed"
        for _ in range(200):
            service.organization._event("curator", "completed")
        assert len(service.organization.events) == 128
    finally:
        release.set()
        await task


async def test_qq_status_is_explicit_and_foreground_flag_resets_on_cancellation(observatory):
    client, store, service, _, _ = observatory
    entered = asyncio.Event()

    async def waiting(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    service.agent.run = waiting
    service.qq_status = lambda: {"gateway": "ready", "consumer": "running"}
    task = asyncio.create_task(service.run("synthetic foreground"))
    await entered.wait()
    try:
        data = (await client.get("/api/status")).json()
        assert data["foreground_active"] and data["qq"]["gateway"] == "ready"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert not service.foreground_active
