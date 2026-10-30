"""Regression coverage for observer startup and maintenance result boundaries."""

import sqlite3

import pytest

from personal_agent.config import Settings
from personal_agent.core.agent import Agent
from personal_agent.core.history import HistoryError
from personal_agent.organization.cycle import CycleProgress, WorkResult
from personal_agent.runtime import ChatService, open_application
from personal_agent.sessions import SessionManager
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.tools.builtin import create_builtin_registry
from tests.fakes import FakeProvider


async def test_observer_startup_never_changes_existing_database(tmp_path):
    path = tmp_path / "observer.db"
    with SQLiteStore(path) as store:
        store.initialize()
        sessions = SessionManager(store)
        selected = sessions.current.id
    before = path.read_bytes()
    settings = Settings(
        api_key="synthetic",
        model="synthetic",
        context_window=131072,
        database_path=path,
        memory_root=tmp_path / "memory",
    )
    async with open_application(settings, observer_only=True) as service:
        assert service.sessions.current.id == selected
        assert service.sessions.store._connection.total_changes == 0
        assert service.organization.task is None
        with pytest.raises(HistoryError):
            service.sessions.new()
        with pytest.raises(HistoryError):
            service.sessions.store.create_agent("forbidden", "forbidden")
    assert path.read_bytes() == before
    assert not settings.memory_root.exists()


@pytest.mark.parametrize("kind", ["missing", "old_schema", "no_agent", "no_conversation"])
async def test_observer_refuses_missing_initialization_without_repair(tmp_path, kind):
    path = tmp_path / "observer.db"
    if kind != "missing":
        with SQLiteStore(path) as store:
            store.initialize()
            if kind == "no_conversation":
                store.create_agent("HeyBuddy", "synthetic", agent_id="heybuddy")
            if kind == "old_schema":
                store._connection.execute("PRAGMA user_version=16")
    before = path.read_bytes() if path.exists() else None
    settings = Settings(
        api_key="synthetic",
        model="synthetic",
        context_window=131072,
        database_path=path,
        memory_root=tmp_path / "memory",
    )
    with pytest.raises(HistoryError):
        async with open_application(settings, observer_only=True):
            pytest.fail("Observer must not initialize or repair persistent data")
    assert (path.read_bytes() if path.exists() else None) == before
    if before is not None:
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


@pytest.mark.parametrize(
    "outcome",
    [
        "no_learning_work",
        "no_proactive_work",
        "no_curator_work",
        "no_character_work",
        "no_recovery_work",
        "stale",
        "blocked",
        "pending",
        "budget_exhausted",
        "future_status",
    ],
)
def test_non_completion_reports_cannot_be_counted_as_success(outcome):
    cycle = CycleProgress(learning_first=True, learning_capacity=True)
    assert not cycle.observe("learning", WorkResult.from_report({"outcome": outcome}))
    assert cycle.batches == 0
    assert not cycle.successes


def test_reconciliation_failure_preserves_error_without_success_timestamp():
    cycle = CycleProgress(learning_first=True, learning_capacity=True)
    assert cycle.observe(
        "learning",
        WorkResult.from_report(
            {
                "outcome": "requires_reconciliation",
                "error_code": "publication_uncertain",
            }
        ),
    )
    assert cycle.error == "publication_uncertain"
    assert cycle.batches == 1
    assert not cycle.successes


def test_service_switches_context_and_temporal_callbacks_keep_their_scope(tmp_path, monkeypatch):
    with SQLiteStore(tmp_path / "sessions.db") as store:
        store.initialize()
        sessions = SessionManager(store)
        first = sessions.current.id
        original = sessions.context()
        service = ChatService(Agent(FakeProvider(), create_builtin_registry(), original), sessions)
        service.select_conversation()
        assert sessions.current.id != first
        assert service.agent.context is not original
        seen = []
        monkeypatch.setattr(store, "last_interaction", lambda cid: seen.append(cid))
        original.temporal()
        service.agent.context.temporal()
        assert seen == [first, sessions.current.id]
        service.select_conversation(first)
        assert sessions.current.id == first
        service.foreground_active = True
        with pytest.raises(RuntimeError):
            service.select_conversation()
        assert sessions.current.id == first
