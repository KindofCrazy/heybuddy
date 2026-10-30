"""Deferred offline verification for P4.4.1; no real transport or model is required."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from personal_agent.proactive.delivery import FakeSender
from personal_agent.scheduler.cli import command
from personal_agent.scheduler.models import (
    ReminderError,
    ReminderGrant,
    ReminderRequest,
    SchedulerPolicy,
)
from personal_agent.scheduler.runtime import SchedulerRuntime
from personal_agent.sessions import SessionManager
from personal_agent.storage import sqlite as storage
from personal_agent.storage.reminders import ReminderRepository
from personal_agent.storage.sqlite import SQLiteStore


class Clock:
    def __init__(self):
        self.time = datetime(2026, 10, 30, 0, 0, tzinfo=UTC)

    def now(self):
        return self.time

    def monotonic(self):
        return 0

    async def sleep(self, seconds):
        await asyncio.Event().wait()


@pytest.fixture
def reminders(tmp_path):
    with SQLiteStore(tmp_path / "reminders.db") as store:
        store.initialize()
        sessions = SessionManager(store)
        clock = Clock()
        runtime = SchedulerRuntime(
            ReminderRepository(store, sessions.agent.id), SchedulerPolicy(), clock=clock
        )
        yield store, sessions, runtime, clock


async def test_due_is_deterministic_blocked_without_channel_and_expires(reminders):
    store, sessions, runtime, clock = reminders
    created = await command(runtime, sessions.current.id, "in 10m 喝水")
    item = created["reminder"]
    assert created["outcome"] == "recorded" and not created["delivery_available"]
    runtime.tick()
    assert runtime.repository.get(item["id"])["status"] == "scheduled"
    clock.time += timedelta(minutes=10)
    runtime.tick()
    assert runtime.repository.get(item["id"])["status"] == "blocked"
    events = runtime.repository.events(item["id"])
    runtime.tick()
    assert runtime.repository.events(item["id"]) == events
    clock.time += timedelta(days=2)
    runtime.tick()
    assert runtime.repository.get(item["id"])["status"] == "expired"
    assert store.list_runs(sessions.current.id) == []


async def test_cancel_and_idempotency_do_not_resurrect(reminders):
    _, sessions, runtime, clock = reminders
    request = ReminderRequest(
        request_id="explicit-1",
        conversation_id=sessions.current.id,
        due_at=clock.now() + timedelta(minutes=1),
        text="提交论文",
    )
    item = runtime.create(request)["reminder"]
    assert runtime.create(request)["reminder"]["id"] == item["id"]
    assert runtime.cancel(item["id"])["outcome"] == "cancelled"
    assert runtime.create(request)["reminder"]["status"] == "cancelled"
    clock.time += timedelta(minutes=2)
    runtime.tick()
    grant = ReminderGrant(enabled=True, conversation_id=sessions.current.id, recipient="local")
    sender = FakeSender()
    assert (await runtime.simulate(item["id"], sender, lambda: grant))["outcome"] == "blocked"
    assert not sender.messages
    with pytest.raises(ReminderError, match="idempotency_conflict"):
        runtime.create(request.model_copy(update={"text": "other"}))


async def test_explicit_simulation_once_and_restart_uncertain(reminders):
    store, sessions, runtime, clock = reminders
    item = (await command(runtime, sessions.current.id, "in 1s 喝水"))["reminder"]
    clock.time += timedelta(seconds=1)
    runtime.tick()
    grant = ReminderGrant(enabled=True, conversation_id=sessions.current.id, recipient="local")
    sender = FakeSender()
    assert (await runtime.simulate(item["id"], sender, lambda: grant))["outcome"] == "accepted"
    assert (await runtime.simulate(item["id"], sender, lambda: grant))["outcome"] == "blocked"
    assert len(sender.messages) == 1
    another = (await command(runtime, sessions.current.id, "in 1s 第二条"))["reminder"]
    clock.time += timedelta(seconds=1)
    assert runtime.repository.claim(another, grant, clock.now())
    reopened = SchedulerRuntime(
        ReminderRepository(store, sessions.agent.id), SchedulerPolicy(), clock=clock
    )
    reopened.tick()
    assert reopened.repository.get(another["id"])["status"] == "uncertain"
    assert (await reopened.simulate(another["id"], sender, lambda: grant))["outcome"] == "blocked"


async def test_scope_and_observer_no_mutations(reminders):
    store, sessions, runtime, _ = reminders
    item = (await command(runtime, sessions.current.id, "in 1m 提醒"))["reminder"]
    foreign = store.create_agent("Other", "synthetic")
    with pytest.raises(ReminderError, match="not_found"):
        ReminderRepository(store, foreign.id).get(item["id"])
    observer = SchedulerRuntime(runtime.repository, SchedulerPolicy(), read_only=True)
    before = store._connection.total_changes
    observer.start()
    assert observer.task is None and not observer.status()["running"]
    with pytest.raises(ReminderError, match="unavailable"):
        observer.cancel(item["id"])
    assert store._connection.total_changes == before


@pytest.mark.parametrize(
    "text", ["at 2026-10-31T09:00:00 无时区", "in 0m 无效", "in 999999d 太远", "at invalid 无效"]
)
async def test_ambiguous_or_invalid_time_never_creates(reminders, text):
    _, sessions, runtime, _ = reminders
    with pytest.raises(ReminderError):
        await command(runtime, sessions.current.id, text)
    assert runtime.repository.listing()["items"] == []


def test_schema_17_to_18_rollback(tmp_path, monkeypatch):
    path = tmp_path / "migration.db"
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 17)
        with SQLiteStore(path) as store:
            store.initialize()
    with monkeypatch.context() as patch:
        patch.setitem(storage.MIGRATIONS, 18, (*storage.MIGRATIONS[18], "INVALID SQL"))
        with SQLiteStore(path) as store:
            from personal_agent.core.history import HistoryError

            with pytest.raises(HistoryError):
                store.initialize()
            assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 17
            assert (
                store._connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='scheduled_reminders'"
                ).fetchone()
                is None
            )
