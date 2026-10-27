import asyncio
import subprocess
import sys
from datetime import timedelta

from personal_agent.organization.runtime import OrganizationRuntime
from personal_agent.sessions import SessionManager
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.sqlite import SQLiteStore
from tests.fakes import FakeProvider
from tests.test_episodes import SegmentProvider, organizer, seed, state
from tests.test_organization_schedule import FakeClock, pump
from tests.test_threads import thread_service

__all__ = ["state"]


async def test_active_conversation_is_not_mechanically_split_by_periodic_checks(state):
    db, stream, repo = state
    clock = FakeClock()
    provider = SegmentProvider()
    runtime = OrganizationRuntime(organizer(repo, provider), clock=clock)
    try:
        runtime.schedule(stream)
        await pump()
        for index in range(3):
            clock.advance(600)
            await runtime.foreground()
            seed(db, stream, text=f"同一段连续对话 {index}", at=clock.now())
            clock.advance(1)
            runtime.schedule(stream)
            await pump()
            assert not provider.requests
        clock.advance(1499)
        await pump()
        assert not provider.requests
        clock.advance(1)
        await pump(lambda: bool(repo.list(stream)))
        assert len(repo.list(stream)) == 1 and len(provider.requests) == 1
        assert len(repo.list(stream)[0].run_ids) == 3
    finally:
        await runtime.close()


async def test_provider_that_swallows_cancel_cannot_publish_late_suggestion(state):
    db, stream, repo = state
    clock = FakeClock()
    seed(db, stream, at=clock.now() - timedelta(hours=1))
    entered = asyncio.Event()

    class Suppressing(SegmentProvider):
        async def generate(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return await super().generate(*args, **kwargs)

    runtime = OrganizationRuntime(organizer(repo, Suppressing()), clock=clock)
    try:
        runtime.schedule(stream)
        await entered.wait()
        await runtime.foreground()
        assert repo.status(stream)["position"] == 0
        assert repo.status(stream)["state"] == "pending" and not repo.list(stream)
        assert db.list_runs(stream)[0].status == "completed"
    finally:
        await runtime.close()


async def test_backward_wall_jump_does_not_delay_live_thread_interval(state):
    db, stream, repo = state
    clock = FakeClock()
    seed(db, stream, at=clock.now())
    await organizer(repo).organize(stream, at=clock.now())
    threads = thread_service(db)
    provider = threads.model.provider
    runtime = OrganizationRuntime(organizer(repo), threads=threads, clock=clock)
    try:
        runtime.schedule(stream)
        await pump()
        # Four actual hours elapse, but the displayed wall time advances only two.
        clock.advance(14400, wall_seconds=7200)
        await pump(lambda: bool(threads.repository.list()))
        assert len(provider.requests) == 1
    finally:
        await runtime.close()


async def test_backward_wall_jump_does_not_extend_backlog_cooldown(state):
    from tests.test_organization_backlog import forty

    db, stream, repo = state
    clock = FakeClock()
    forty(db, stream, clock)
    provider = SegmentProvider()
    runtime = OrganizationRuntime(organizer(repo, provider, maintenance_batches=1), clock=clock)
    try:
        runtime.schedule(stream)
        await pump(lambda: bool(runtime.last_report.get("cycle")))
        assert len(provider.requests) == 1
        clock.advance(60, wall_seconds=-3600)
        await pump(lambda: len(provider.requests) == 2)
        assert len(provider.requests) == 2
    finally:
        await runtime.close()


async def test_finalize_deadline_is_not_blocked_by_failed_pending_batch(state):
    from personal_agent.core.models import ModelResponse

    db, stream, repo = state
    clock = FakeClock()
    start = clock.now() - timedelta(hours=4)
    seed(db, stream, at=start)
    await organizer(repo).organize(stream, at=start + timedelta(seconds=1))
    episode = repo.list(stream)[0]
    assert episode.status == "provisional"
    seed(db, stream, at=start + timedelta(minutes=15))
    assert (await organizer(repo, FakeProvider(ModelResponse(text="invalid"))).organize(stream))[
        "outcome"
    ] == "failed"
    provider = FakeProvider()
    runtime = OrganizationRuntime(organizer(repo, provider), clock=clock)
    try:
        runtime.schedule(stream)
        await pump(lambda: repo.get(episode.id).status == "finalized")
        assert repo.get(episode.id).status == "finalized"
        assert repo.status(stream)["state"] == "failed" and not provider.requests
    finally:
        await runtime.close()


async def test_real_process_exit_after_repair_page_resumes_automatically_when_idle(tmp_path):
    path = tmp_path / "repair-process.db"
    script = r"""
import asyncio, json, os, sys
from pathlib import Path
from personal_agent.core.budget import ContextBudget
from personal_agent.core.models import AssistantMessage, TextMessage, RunResult, ModelResponse
from personal_agent.organization.episodes import EpisodeOrganizer, EPISODE_PROMPT
from personal_agent.organization.threads import ThreadOrganizer, THREAD_PROMPT
from personal_agent.organization.models import OrganizationPolicy
from personal_agent.organization.provider import OrganizerModel
from personal_agent.sessions import SessionManager
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.threads import ThreadRepository

class Provider:
    async def generate(self, messages, tools, **kwargs):
        data = json.loads(messages[1].text)
        if messages[0].text == EPISODE_PROMPT:
            output = {"episodes": [{"run_ids": [r["run_id"]], "title": "Topic",
                       "summary": "source " + str(i), "highlights": []}
                       for i, r in enumerate(data["runs"])]}
        elif messages[0].text == THREAD_PROMPT:
            output = {"groups": [{"episode_ids": [e["id"] for e in data["episodes"]],
                       "title": "Topic", "current_state": "old state"}], "no_thread": []}
        else:
            output = {"current_state": data["previous_rebuild"] + " ".join(
                       e["summary"] for e in data["episodes"]), "status": "active"}
        return ModelResponse(text=json.dumps(output))

async def run():
    with SQLiteStore(Path(sys.argv[1])) as db:
        db.initialize()
        sessions = SessionManager(db)
        for text in ("source A", "source B"):
            rid = db.begin_run(sessions.current.id, TextMessage(role="user", text=text))
            db.append_message(rid, AssistantMessage(text="reply"))
            db.finish_run(rid, RunResult(status="completed", rounds=1))
        provider = Provider()
        budget = ContextBudget(context_window=64000)
        episodes = EpisodeRepository(db, sessions.agent.id)
        episode_service = EpisodeOrganizer(episodes,
            OrganizerModel(provider,budget,OrganizationPolicy()))
        await episode_service.organize(sessions.current.id)
        threads = ThreadRepository(db, sessions.agent.id)
        thread_model = OrganizerModel(provider,budget,OrganizationPolicy())
        await ThreadOrganizer(threads,thread_model).organize()
        target = episodes.list(sessions.current.id)[0]
        target.summary += " corrected"
        target.revision += 1
        target.thread_dirty = True
        with db._transaction() as connection:
            episodes._write(connection,target,existing=True)
        repair_model = OrganizerModel(provider,budget,OrganizationPolicy(page_runs=1))
        service = ThreadOrganizer(threads,repair_model)
        assert (await service.repair())["outcome"] == "repair_page"
        os._exit(31)
asyncio.run(run())
"""
    process = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-u", "-c", script, str(path)],
        capture_output=True,
        timeout=15,
    )
    assert process.returncode == 31
    with SQLiteStore(path) as db:
        db.initialize()
        stream = SessionManager(db).current.id
        repo = EpisodeRepository(db, "heybuddy")
        threads = thread_service(db, page_runs=1)
        threads.repository.recover()
        target = threads.repository.list()[0]
        before = db.load_messages(stream)
        clock = FakeClock()
        runtime = OrganizationRuntime(organizer(repo), threads=threads, clock=clock)
        try:
            runtime.schedule(stream)
            await pump()
            clock.advance(1501)
            await pump(lambda: not threads.repository.get(target.id).dirty)
            assert not threads.repository.get(target.id).dirty
            assert db.load_messages(stream) == before
            with db._transaction() as connection:
                assert (
                    connection.execute(
                        "SELECT source_count FROM thread_repairs WHERE thread_id=?", (target.id,)
                    ).fetchone()[0]
                    == 2
                )
        finally:
            await runtime.close()
