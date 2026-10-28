import asyncio
import json
import threading

from personal_agent.cli_commands import MaintenanceCommands
from personal_agent.core.agent import Agent
from personal_agent.core.models import ModelResponse
from personal_agent.main import chat
from personal_agent.organization.runtime import OrganizationRuntime
from personal_agent.sessions import SessionManager
from personal_agent.tools.builtin import create_builtin_registry
from tests.fakes import FakeProvider
from tests.test_episodes import SegmentProvider, organizer, seed, state

__all__ = ["state"]


async def test_idle_trigger_and_foreground_cancel_never_wait_for_model(state):
    db, stream, repo = state
    seed(db, stream)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class Slow:
        async def generate(self, *args, **kwargs):
            assert not db._connection.in_transaction
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    runtime = OrganizationRuntime(organizer(repo, Slow(), idle_seconds=0.01))
    try:
        runtime.schedule(stream)
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(runtime.foreground(), 1)
        assert cancelled.is_set() and repo.status(stream)["position"] == 0
        assert db.list_runs(stream)[0].status == "completed"
    finally:
        await runtime.close()


async def test_count_trigger_restart_and_cli_debug_commands(state):
    db, stream, repo = state
    seed(db, stream)
    provider = SegmentProvider()
    runtime = OrganizationRuntime(organizer(repo, provider, run_threshold=1))
    runtime.schedule(stream)
    for _ in range(100):
        if repo.list(stream):
            break
        await asyncio.sleep(0.01)
    assert len(repo.list(stream)) == 1 and len(provider.requests) == 1
    await runtime.foreground()
    output = []
    await MaintenanceCommands(runtime).command(stream, "/episode status", output.append)
    await MaintenanceCommands(runtime).command(stream, "/episode list", output.append)
    episode_id = repo.list(stream)[0].id
    await MaintenanceCommands(runtime).command(stream, f"/episode show {episode_id}", output.append)
    await MaintenanceCommands(runtime).command(stream, "/episode organize", output.append)
    assert json.loads(output[-1])["outcome"] == "no_history"
    assert json.loads(output[2])["id"] == episode_id
    await runtime.close()


async def test_async_cli_answer_precedes_organization_and_new_input_preempts(state):
    db, stream, repo = state
    sessions = SessionManager(db)
    main = FakeProvider(ModelResponse(text="answer one"), ModelResponse(text="answer two"))
    agent = Agent(main, create_builtin_registry(), context=sessions.context())
    started = threading.Event()
    cancelled = asyncio.Event()
    output = []

    class Slow:
        async def generate(self, *args, **kwargs):
            assert "Agent > answer one" in output
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    runtime = OrganizationRuntime(organizer(repo, Slow(), run_threshold=1))
    calls = 0

    def read(prompt):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "first"
        if calls == 2:
            assert started.wait(2)
            return "second"
        return "quit"

    await asyncio.wait_for(
        chat(agent, read, output.append, sessions=sessions, organization=runtime), 5
    )
    assert cancelled.is_set()
    assert "Agent > answer two" in output
    assert all(r.status == "completed" for r in db.list_runs(stream))
    assert not repo.list(stream)


async def test_input_eof_and_cancel_close_without_executor_shutdown(state):
    _, _, repo = state
    runtime = OrganizationRuntime(organizer(repo))

    def read(prompt):
        raise EOFError

    output = []
    await chat(
        Agent(FakeProvider(), create_builtin_registry()), read, output.append, organization=runtime
    )
    assert output[-1] == "再见。"


async def test_diagnostics_without_schedule_never_initializes_state(state):
    db, stream, repo = state
    runtime = OrganizationRuntime(organizer(repo))
    before = db._connection.total_changes
    try:
        for _ in range(2):
            assert runtime.diagnostics(stream)["stream_id"] == stream
            assert runtime.diagnostics(stream, read_only=True)["schedule"] is None
        assert db._connection.total_changes == before
    finally:
        await runtime.close()


async def test_maintenance_construction_does_not_start_console_thread(state, monkeypatch):
    _, _, repo = state
    original = threading.Thread.start

    def start(thread):
        assert thread.name != "heybuddy-input"
        return original(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    runtime = OrganizationRuntime(organizer(repo))
    await runtime.close()


async def test_diagnostics_for_selected_stream_only_projects_selection(state):
    db, stream, repo = state
    runtime = OrganizationRuntime(organizer(repo))
    runtime.schedule_repository.ensure(stream, runtime.clock.now(), 14400, activity=False)
    selected = db.create_conversation(repo.agent_id).id
    before = db._connection.total_changes
    try:
        assert runtime.diagnostics(selected)["stream_id"] == selected
        assert runtime.diagnostics(selected, read_only=True)["schedule"]["stream_id"] == stream
        assert db._connection.total_changes == before
    finally:
        await runtime.close()
