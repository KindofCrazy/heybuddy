import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from personal_agent.core.models import AssistantMessage, RunResult
from personal_agent.organization.runtime import OrganizationRuntime
from personal_agent.storage.schedule import ScheduleRepository
from tests.test_episodes import SegmentProvider, organizer, seed, state
from tests.test_threads import thread_service

__all__ = ["state"]


class FakeClock:
    def __init__(self, now=None):
        self.wall = now or datetime.now(UTC)
        self.ticks = 0.0
        self.waiters = []

    def now(self):
        return self.wall

    def monotonic(self):
        return self.ticks

    def advance(self, seconds, *, wall_seconds=None):
        self.ticks += seconds
        self.wall += timedelta(seconds=seconds if wall_seconds is None else wall_seconds)
        for deadline, future in self.waiters:
            if deadline <= self.ticks and not future.done():
                future.set_result(None)
        self.waiters = [(d, f) for d, f in self.waiters if not f.done()]

    async def sleep(self, seconds):
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((self.ticks + seconds, future))
        await future


async def pump(predicate=lambda: False):
    for _ in range(100):
        await asyncio.sleep(0)
        if predicate():
            return


async def test_single_pending_run_organizes_at_idle_not_at_its_start_time(state):
    db, stream, repo = state
    clock = FakeClock()
    run_id = seed(db, stream, at=clock.now() - timedelta(hours=4))
    with db._transaction() as connection:
        connection.execute(
            "UPDATE runs SET ended_at=? WHERE id=?", (clock.now().isoformat(), run_id)
        )
    provider = SegmentProvider()
    runtime = OrganizationRuntime(organizer(repo, provider), clock=clock)
    try:
        runtime.schedule(stream)
        await pump()
        assert not provider.requests
        clock.advance(1499)
        await pump()
        assert not provider.requests
        clock.advance(1)
        await pump(lambda: bool(repo.list(stream)))
        assert len(provider.requests) == 1 and len(repo.list(stream)) == 1
    finally:
        await runtime.close()


async def test_running_run_blocks_model_even_when_counts_and_time_are_due(state):
    db, stream, repo = state
    clock = FakeClock()
    seed(db, stream)
    active = seed(db, stream, status="running")
    provider = SegmentProvider()
    runtime = OrganizationRuntime(organizer(repo, provider, run_threshold=1), clock=clock)
    try:
        runtime.schedule(stream)
        await pump()
        clock.advance(14400)
        await pump()
        assert not provider.requests
        db.append_message(active, AssistantMessage(text="long tool finally finished"))
        db.finish_run(active, RunResult(status="completed", rounds=1))
        with db._transaction() as connection:
            connection.execute(
                "UPDATE runs SET ended_at=? WHERE id=?", (clock.now().isoformat(), active)
            )
        clock.advance(60)
        await pump(lambda: bool(repo.list(stream)))
        assert len(provider.requests) == 1  # Count threshold can trigger after completion.
    finally:
        await runtime.close()


async def test_one_episode_receives_thread_opportunity_at_four_hours_without_more_input(state):
    db, stream, repo = state
    clock = FakeClock()
    seed(db, stream, at=clock.now())
    await organizer(repo).organize(stream, at=clock.now())
    threads = thread_service(db)
    thread_provider = threads.model.provider
    runtime = OrganizationRuntime(organizer(repo), threads=threads, clock=clock)
    try:
        runtime.schedule(stream)
        await pump()
        assert not thread_provider.requests
        clock.advance(14401)
        await pump(lambda: bool(threads.repository.list()))
        assert len(thread_provider.requests) == 1
        assert runtime.diagnostics(stream)["last_thread_success_at"]
    finally:
        await runtime.close()


async def test_empty_periodic_checks_never_call_model_and_advance_due_time(state):
    db, stream, repo = state
    clock = FakeClock()
    provider = SegmentProvider()
    threads = thread_service(db)
    runtime = OrganizationRuntime(organizer(repo, provider), threads=threads, clock=clock)
    try:
        runtime.schedule(stream)
        await pump()
        clock.advance(14400)
        await pump()
        assert not provider.requests and not threads.model.provider.requests
        assert datetime.fromisoformat(runtime.diagnostics(stream)["thread_due_at"]) > clock.now()
    finally:
        await runtime.close()


async def test_due_provisional_finalizes_without_creating_an_episode_or_request(state):
    db, stream, repo = state
    clock = FakeClock()
    seed(db, stream, at=clock.now())
    await organizer(repo).organize(stream, at=clock.now())
    episode = repo.list(stream)[0]
    assert episode.status == "provisional"
    provider = SegmentProvider()
    runtime = OrganizationRuntime(organizer(repo, provider), clock=clock)
    try:
        runtime.schedule(stream)
        await pump()
        clock.advance(10801)
        await pump(lambda: repo.get(episode.id).status == "finalized")
        assert repo.get(episode.id).status == "finalized"
        assert len(repo.list(stream)) == 1 and not provider.requests
    finally:
        await runtime.close()


async def test_dirty_thread_repairs_at_idle_without_waiting_for_thread_interval(state):
    from tests.test_thread_repair import linked, move

    db, stream, repo = state
    threads, a, _, _ = await linked(state)
    move(threads, a, None)
    clock = FakeClock()
    schedule = ScheduleRepository(repo)
    schedule.ensure(stream, clock.now(), 14400, activity=True)
    runtime = OrganizationRuntime(organizer(repo), threads=threads, clock=clock)
    try:
        runtime.schedule(stream)
        await pump()
        assert threads.repository.get(a.thread_id).dirty
        clock.advance(1500)
        await pump(lambda: not threads.repository.get(a.thread_id).dirty)
        assert not threads.repository.get(a.thread_id).dirty
        assert clock.now() < datetime.fromisoformat(runtime.diagnostics(stream)["thread_due_at"])
    finally:
        await runtime.close()


async def test_idle_monotonic_guard_activity_resets_and_schedule_is_idempotent(state):
    db, stream, repo = state
    clock = FakeClock()
    seed(db, stream, at=clock.now())
    provider = SegmentProvider()
    runtime = OrganizationRuntime(organizer(repo, provider), clock=clock)
    try:
        runtime.schedule(stream)
        first = runtime.task
        runtime.schedule(stream)
        assert runtime.task is first
        await pump()
        clock.advance(1, wall_seconds=14400)
        await pump()
        assert not provider.requests  # A wall-clock jump does not manufacture 25 minutes Idle.
        await runtime.foreground()
        runtime.schedule(stream)
        await pump()
        clock.advance(1499)
        await pump()
        assert not provider.requests
        clock.advance(1)
        await pump(lambda: bool(repo.list(stream)))
        assert len(provider.requests) == 1
    finally:
        await runtime.close()


async def test_persisted_thread_deadline_survives_restart(state):
    db, stream, repo = state
    clock = FakeClock()
    seed(db, stream, at=clock.now())
    await organizer(repo).organize(stream, at=clock.now())
    first = OrganizationRuntime(organizer(repo), threads=thread_service(db), clock=clock)
    first.schedule(stream)
    await pump()
    due = first.diagnostics(stream)["thread_due_at"]
    await first.close()
    clock.advance(14500)
    second = OrganizationRuntime(organizer(repo), threads=thread_service(db), clock=clock)
    try:
        second.schedule(stream)
        assert second.diagnostics(stream)["thread_due_at"] == due
        await pump(lambda: bool(second.threads.repository.list()))
        assert second.threads.repository.list()
    finally:
        await second.close()


def test_schema_nine_rolls_back_deadline_state_without_touching_journal(tmp_path, monkeypatch):
    from personal_agent.core.history import HistoryError
    from personal_agent.storage import sqlite as storage

    path = tmp_path / "eight.db"
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 8)
        with storage.SQLiteStore(path) as db:
            db.initialize()
    with storage.SQLiteStore(path) as db:
        monkeypatch.setitem(storage.MIGRATIONS, 9, (*storage.MIGRATIONS[9], "INVALID SQL"))
        with pytest.raises(HistoryError):
            db.initialize()
        assert db._connection.execute("PRAGMA user_version").fetchone()[0] == 8
