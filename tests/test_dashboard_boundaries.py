import os
from datetime import UTC, datetime

import httpx

from personal_agent.config import Settings
from personal_agent.dashboard.app import create_app
from personal_agent.organization.models import ThreadDecision, ThreadGroup
from personal_agent.runtime import open_application
from personal_agent.sessions import SessionManager
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.learning import LearningRepository
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.storage.threads import ThreadRepository
from tests.test_dashboard import observatory
from tests.test_episodes import organizer, seed

__all__ = ["observatory"]


async def test_all_detail_families_recheck_foreign_scope(observatory):
    client, store, _, _, _ = observatory
    other = store.create_agent("Other", "prompt", agent_id="other")
    stream = store.create_conversation(other.id)
    rid = seed(store, stream.id)
    episodes = EpisodeRepository(store, other.id)
    await organizer(episodes).organize(stream.id)
    episode = episodes.list(stream.id)[0]
    threads = ThreadRepository(store, other.id)
    revision, sources, candidates = threads.snapshot(16)
    threads.publish(
        revision,
        sources,
        candidates,
        ThreadDecision(
            groups=[
                ThreadGroup(
                    episode_ids=[episode.id],
                    title="Private other Thread",
                    current_state="Private other state",
                )
            ]
        ),
        {},
    )
    tid = threads.list()[0].id
    jid = LearningRepository(episodes).create(
        "priority", {"runs": [{"id": rid}], "episodes": []}, datetime.now(UTC)
    )
    for route in (
        f"/api/runs/{rid}/messages",
        f"/api/episodes/{episode.id}",
        f"/api/threads/{tid}",
        f"/api/learning/jobs/{jid}",
        f"/api/episodes?thread_id={tid}",
        f"/api/runs?episode_id={episode.id}",
    ):
        assert (await client.get(route)).status_code == 404
    for route in ("/api/episodes", "/api/threads", "/api/learning/jobs", "/api/activity"):
        response = await client.get(route)
        assert all(key not in response.text for key in (rid, tid, jid, episode.id))


async def test_memory_hardlinks_hash_pages_and_numeric_limits(observatory, tmp_path):
    client, _, service, _, _ = observatory
    memory = service.sessions.memory
    folder = memory.root / "topics"
    folder.mkdir()
    external = tmp_path / "external.md"
    external.write_text("private external content", encoding="utf-8")
    os.link(external, folder / "linked.md")
    assert (await client.get("/api/memory/file?path=topics/linked.md")).status_code == 404
    assert "linked.md" not in (await client.get("/api/memory?directory=topics")).text
    long_file = folder / "long.md"
    long_file.write_text("# Topic\n" + "evidence\n" * 1000, encoding="utf-8")
    first = (await client.get("/api/memory/file?path=topics/long.md")).json()
    assert first["truncated"] and first["next_offset"] == 4096
    long_file.write_text("changed", encoding="utf-8")
    response = await client.get(
        "/api/memory/file",
        params={
            "path": "topics/long.md",
            "offset": first["next_offset"],
            "expected_hash": first["hash"],
        },
    )
    assert response.status_code == 404
    for route in ("/api/runs", "/api/episodes", "/api/threads", "/api/learning/jobs"):
        assert (await client.get(route, params={"cursor": 2**80})).status_code == 422


async def test_restart_observer_preserves_unresolved_run_without_model_requests(tmp_path):
    path = tmp_path / "restart.db"
    with SQLiteStore(path) as store:
        store.initialize()
        sessions = SessionManager(store)
        rid = seed(store, sessions.current.id)
        active = seed(store, sessions.current.id, status="running")
    settings = Settings(
        api_key="synthetic",
        model="synthetic",
        context_window=131072,
        database_path=path,
        memory_root=tmp_path / "absent-memory",
    )
    async with open_application(settings, observer_only=True) as service:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)), base_url="http://127.0.0.1:8765"
        ) as client:
            assert (await client.get(f"/api/runs/{rid}")).json()["status"] == "completed"
            assert (await client.get(f"/api/runs/{active}")).json()["status"] == "running"
            status = (await client.get("/api/status")).json()
            assert status["state"] == "Unknown" and not status["foreground_active"]
            assert status["observer_only"]
        assert not service.sessions.memory.root.exists()


async def test_activity_orders_instants_across_different_utc_offsets(observatory):
    client, store, service, _, _ = observatory
    earlier = seed(store, service.sessions.current.id)
    later = seed(store, service.sessions.current.id)
    with store._transaction() as db:
        db.execute(
            "UPDATE runs SET started_at=?,ended_at=? WHERE id=?",
            ("2026-10-28T10:00:00+08:00", "2026-10-28T10:00:01+08:00", earlier),
        )
        db.execute(
            "UPDATE runs SET started_at=?,ended_at=? WHERE id=?",
            ("2026-10-28T03:00:00+00:00", "2026-10-28T03:00:01+00:00", later),
        )
    events = (await client.get("/api/activity")).json()["items"]
    assert [e["related_object_id"] for e in events] == [later, later, earlier, earlier]
