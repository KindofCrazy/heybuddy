import asyncio
from datetime import timedelta

from personal_agent.organization.runtime import OrganizationRuntime
from personal_agent.sessions import SessionManager
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.sqlite import SQLiteStore
from tests.fakes import FakeProvider
from tests.test_episodes import SegmentProvider, organizer, seed, state
from tests.test_organization_schedule import FakeClock, pump
from tests.test_thread_repair import linked, move
from tests.test_threads import TopicProvider, thread_service

__all__ = ["state"]


def forty(db, stream, clock):
    start = clock.now() - timedelta(hours=1)
    return [seed(db, stream, at=start + timedelta(seconds=index * 2)) for index in range(40)]


async def test_forty_runs_drain_across_request_budgets_and_cooldowns_without_new_input(state):
    db, stream, repo = state
    clock = FakeClock()
    ids = forty(db, stream, clock)
    before = db.load_messages(stream)
    provider = SegmentProvider()
    runtime = OrganizationRuntime(organizer(repo, provider, maintenance_requests=2), clock=clock)
    try:
        runtime.schedule(stream)
        await pump(lambda: bool(runtime.last_report.get("cycle")))
        assert len(provider.requests) == 2
        assert sum(len(e.run_ids) for e in repo.list(stream)) == 16
        assert repo.status(stream)["state"] == "pending"  # Budget != failed.
        clock.advance(59)
        await pump()
        assert len(provider.requests) == 2
        for _ in range(4):
            clock.advance(60)
            await pump()
        assert repo.status(stream)["pending_runs_capped"] == 0
        archived = [rid for e in repo.list(stream) for rid in e.run_ids]
        assert set(archived) == set(ids) and len(archived) == 40
        assert len(provider.requests) == 5
        assert db.load_messages(stream) == before
        assert runtime.schedule_repository.state(stream)["last_requests"] <= 2
    finally:
        await runtime.close()


async def test_backlog_reopen_resumes_after_committed_batch(tmp_path):
    clock = FakeClock()
    path = tmp_path / "restart.db"
    with SQLiteStore(path) as db:
        db.initialize()
        stream = SessionManager(db).current.id
        ids = forty(db, stream, clock)
        repo = EpisodeRepository(db, "heybuddy")
        runtime = OrganizationRuntime(organizer(repo, maintenance_batches=1), clock=clock)
        runtime.schedule(stream)
        await pump(lambda: bool(runtime.last_report.get("cycle")))
        assert sum(len(e.run_ids) for e in repo.list(stream)) == 8
        await runtime.close()
    clock.advance(61)
    with SQLiteStore(path) as db:
        db.initialize()
        repo = EpisodeRepository(db, "heybuddy")
        repo.recover()
        provider = SegmentProvider()
        runtime = OrganizationRuntime(organizer(repo, provider), clock=clock)
        try:
            runtime.schedule(stream)
            await pump(lambda: repo.status(stream)["pending_runs_capped"] == 0)
            assert len(provider.requests) == 4
            assert set(r for e in repo.list(stream) for r in e.run_ids) == set(ids)
        finally:
            await runtime.close()


async def test_cancel_second_batch_keeps_first_and_restores_provider(state):
    db, stream, repo = state
    clock = FakeClock()
    forty(db, stream, clock)
    entered = asyncio.Event()

    class SlowSecond(SegmentProvider):
        async def generate(self, *args, **kwargs):
            if self.requests:
                entered.set()
                await asyncio.Event().wait()
            return await super().generate(*args, **kwargs)

    provider = SlowSecond()
    service = organizer(repo, provider)
    runtime = OrganizationRuntime(service, clock=clock)
    try:
        runtime.schedule(stream)
        await entered.wait()
        assert sum(len(e.run_ids) for e in repo.list(stream)) == 8
        await runtime.foreground()
        assert service.model.provider is provider
        assert repo.status(stream)["state"] == "pending"
        assert sum(len(e.run_ids) for e in repo.list(stream)) == 8
        assert len(db.list_runs(stream)) == 40
    finally:
        await runtime.close()


async def test_failed_batch_is_not_repeated_on_idle_ticks(state):
    db, stream, repo = state
    clock = FakeClock()
    forty(db, stream, clock)
    from personal_agent.core.models import ModelResponse

    provider = FakeProvider(ModelResponse(text="not JSON"))
    runtime = OrganizationRuntime(organizer(repo, provider), clock=clock)
    try:
        runtime.schedule(stream)
        await pump(lambda: repo.status(stream)["state"] == "failed")
        for _ in range(10):
            clock.advance(3600)
            await pump()
        assert len(provider.requests) == 1 and repo.status(stream)["position"] == 0
    finally:
        await runtime.close()


async def test_pending_thread_backlog_drains_in_bounded_cycles(state):
    db, stream, repo = state
    clock = FakeClock()
    for i in range(20):
        seed(db, stream, at=clock.now() - timedelta(days=21 - i))
    episode = organizer(repo)
    while repo.status(stream)["pending_runs_capped"]:
        assert (await episode.organize(stream))["outcome"] == "completed"
    provider = TopicProvider()
    threads = thread_service(db, provider)
    runtime = OrganizationRuntime(
        organizer(repo, maintenance_requests=2), threads=threads, clock=clock
    )
    try:
        runtime.schedule(stream)
        await pump(lambda: bool(runtime.last_report.get("cycle")))
        assert len(provider.requests) == 2
        clock.advance(60)
        await pump(lambda: not threads.repository.status()["pending_episodes_capped"])
        assert threads.repository.status()["pending_episodes_capped"] == 0
        assert len(provider.requests) == 3
    finally:
        await runtime.close()


async def test_cycle_timeout_releases_running_state_without_advancing_sources(state):
    db, stream, repo = state
    clock = FakeClock()
    forty(db, stream, clock)
    entered = asyncio.Event()

    class Slow:
        async def generate(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    runtime = OrganizationRuntime(organizer(repo, Slow(), maintenance_seconds=0.01), clock=clock)
    try:
        runtime.schedule(stream)
        await entered.wait()
        await asyncio.sleep(0.03)
        assert runtime.last_report["cycle"]["time_limited"]
        assert repo.status(stream)["state"] == "pending" and repo.status(stream)["position"] == 0
    finally:
        await runtime.close()


async def test_unfit_single_request_stops_without_paid_repetition(state):
    db, stream, repo = state
    clock = FakeClock()
    forty(db, stream, clock)
    provider = SegmentProvider()
    runtime = OrganizationRuntime(organizer(repo, provider, maintenance_tokens=4096), clock=clock)
    try:
        runtime.schedule(stream)
        await pump(lambda: repo.status(stream)["state"] == "failed")
        clock.advance(7200)
        await pump()
        assert not provider.requests
        assert repo.status(stream)["error_code"] == "maintenance_request_budget"
    finally:
        await runtime.close()


async def test_dirty_repair_retries_after_persisted_backoff_then_recovers(state):
    db, stream, repo = state
    threads, a, _, _ = await linked(state)
    move(threads, a, None)
    clock = FakeClock()
    calls = 0

    class Transient(TopicProvider):
        async def generate(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("private")
            return await super().generate(*args, **kwargs)

    threads.model.provider = Transient()
    threads.policy = threads.policy.model_copy(update={"repair_retry_seconds": 10})
    runtime = OrganizationRuntime(
        organizer(repo, cooldown_seconds=1, check_seconds=1), threads=threads, clock=clock
    )
    try:
        runtime.schedule_repository.ensure(stream, clock.now(), 14400, activity=True)
        runtime.schedule(stream)
        await pump()
        clock.advance(1500)
        await pump(lambda: calls == 1 and runtime.current_stage is None)
        assert threads.repository.get(a.thread_id).dirty
        clock.advance(9)
        await pump()
        assert calls == 1
        clock.advance(1)
        await pump(lambda: not threads.repository.get(a.thread_id).dirty)
        assert calls == 2 and not threads.repository.get(a.thread_id).dirty
    finally:
        await runtime.close()


async def test_repair_repeated_failures_stop_automatic_paid_retries(state):
    db, stream, repo = state
    threads, a, _, _ = await linked(state)
    move(threads, a, None)
    clock = FakeClock()
    calls = 0

    class Failing:
        async def generate(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            raise TimeoutError("private")

    threads.model.provider = Failing()
    threads.policy = threads.policy.model_copy(
        update={"repair_retry_seconds": 1, "repair_max_failures": 2}
    )
    runtime = OrganizationRuntime(
        organizer(repo, cooldown_seconds=1, check_seconds=1), threads=threads, clock=clock
    )
    try:
        runtime.schedule(stream)
        await pump(lambda: calls == 1)
        for _ in range(5):
            clock.advance(3600)
            await pump()
        assert calls == 2 and threads.repository.get(a.thread_id).dirty
        assert threads.repository.status()["last_repair_error"] == "provider_failed"
        threads.model.provider = TopicProvider()
        assert (await threads.repair())["outcome"] == "repaired"
    finally:
        await runtime.close()


async def test_user_preemption_cancels_runtime_dirty_repair_and_allows_normal_run(state):
    from personal_agent.core.agent import Agent
    from personal_agent.core.models import ModelResponse
    from personal_agent.tools.builtin import create_builtin_registry

    db, stream, repo = state
    threads, a, _, _ = await linked(state)
    move(threads, a, None)
    before = db.load_messages(stream)
    entered = asyncio.Event()

    class Slow:
        async def generate(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    threads.model.provider = Slow()
    runtime = OrganizationRuntime(organizer(repo), threads=threads, clock=FakeClock())
    try:
        runtime.schedule(stream)
        await entered.wait()
        assert runtime.current_stage == "repair"
        await runtime.foreground()
        provider = FakeProvider(ModelResponse(text="foreground answer"))
        agent = Agent(provider, create_builtin_registry(), context=SessionManager(db).context())
        assert (await agent.run("new user input")).text == "foreground answer"
        assert db.load_messages(stream)[: len(before)] == before
        assert threads.repository.get(a.thread_id).dirty
        assert runtime.current_stage is None
    finally:
        await runtime.close()


async def test_repair_checkpoint_and_failed_retry_deadline_survive_store_reopen(tmp_path):

    clock = FakeClock()
    path = tmp_path / "repair-restart.db"
    with SQLiteStore(path) as db:
        db.initialize()
        stream = SessionManager(db).current.id
        repo = EpisodeRepository(db, "heybuddy")
        service, a, b, _ = await linked((db, stream, repo))
        revised = repo.get(b.id)
        revised.summary += " corrected"
        revised.revision += 1
        revised.thread_dirty = True
        with db._transaction() as connection:
            repo._write(connection, revised, existing=True)
        service = thread_service(db, page_runs=1)
        assert (await service.repair())["outcome"] == "repair_page"
        service.model.provider = FakeProvider(TimeoutError("private"))
        assert (await service.repair(at=clock.now()))["outcome"] == "failed"
        thread_id = a.thread_id
    with SQLiteStore(path) as db:
        db.initialize()
        service = thread_service(db, page_runs=1)
        service.repository.recover()
        assert (await service.repair(retry=False, at=clock.now()))["outcome"] == "no_dirty_work"
        clock.advance(301)
        assert (await service.repair(retry=False, at=clock.now()))["outcome"] == "repaired"
        assert not service.repository.get(thread_id).dirty
        with db._transaction() as connection:
            assert (
                connection.execute(
                    "SELECT source_count FROM thread_repairs WHERE thread_id=?", (thread_id,)
                ).fetchone()[0]
                == 2
            )


def test_schema_ten_retry_state_migration_rolls_back(tmp_path, monkeypatch):
    import pytest

    from personal_agent.core.history import HistoryError
    from personal_agent.storage import sqlite as storage

    path = tmp_path / "nine.db"
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 9)
        with storage.SQLiteStore(path) as db:
            db.initialize()
    with storage.SQLiteStore(path) as db:
        monkeypatch.setitem(storage.MIGRATIONS, 10, (*storage.MIGRATIONS[10], "INVALID SQL"))
        with pytest.raises(HistoryError):
            db.initialize()
        assert db._connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert "failure_count" not in {
            r[1] for r in db._connection.execute("PRAGMA table_info(thread_repairs)")
        }
