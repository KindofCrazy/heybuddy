"""Offline contracts for online Thread ownership and post-hoc organization."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from personal_agent.cli_commands import session_command
from personal_agent.config import Settings
from personal_agent.core.agent import Agent
from personal_agent.core.budget import ContextBudget
from personal_agent.core.execution import ExecutionBudget
from personal_agent.core.history import HistoryError
from personal_agent.core.models import AssistantMessage, ModelResponse, RunResult, TextMessage
from personal_agent.harness import HarnessRuntime
from personal_agent.organization.router import ThreadRouter
from personal_agent.sessions import SessionManager
from personal_agent.storage import context
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.routing import RoutingRepository
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.storage.thread_history import SQLiteThreadHistory
from personal_agent.tools.builtin import create_builtin_registry
from tests.fakes import FakeProvider, add_call
from tests.test_episodes import SegmentProvider, organizer
from tests.test_threads import TopicProvider, thread_service


@pytest.fixture
def routed(tmp_path):
    with SQLiteStore(tmp_path / "routing.db") as store:
        store.initialize()
        sessions = SessionManager(store, budget=ContextBudget(context_window=131072))
        provider = FakeProvider()
        agent = Agent(provider, create_builtin_registry(), sessions.context())
        settings = Settings(
            api_key="fake",
            model="fake",
            context_window=131072,
            workspace_root=tmp_path / "workspace",
            skills_root=tmp_path / "skills",
        )
        runtime = HarnessRuntime(settings, sessions, agent)
        yield runtime, provider
        assert not runtime.execution.tasks


def repository(runtime, source="cli"):
    return RoutingRepository(
        runtime.sessions.store,
        runtime.sessions.agent.id,
        runtime.sessions.current.id,
        source,
    )


def completed(repo, thread, text):
    store = repo.store
    run = store.begin_run(repo.conversation_id, TextMessage(role="user", text=text))
    repo.bind(run, thread.id, "test")
    store.append_message(run, AssistantMessage(text="answer " + text))
    store.finish_run(run, RunResult(status="completed", rounds=1))
    return run


def texts(request):
    return "\n".join(m.text for m in request if hasattr(m, "text"))


async def test_a_b_a_replays_original_tools_without_other_topic(routed):
    runtime, provider = routed
    provider.responses += [
        ModelResponse(
            tool_calls=[add_call("source-sum")], provider_data={"opaque": [{"value": "keep"}]}
        ),
        ModelResponse(text="A answer"),
        ModelResponse(text="B answer"),
        ModelResponse(text="A resumed"),
    ]
    conversation = runtime.sessions.current.id
    first = await runtime.run("ProjectAlpha 预算五元", frozenset())
    thread_a = repository(runtime).active()
    assert first.status == "completed" and thread_a.current_state is None
    await runtime.run("另外，ProjectBeta 预算十元", frozenset())
    thread_b = repository(runtime).active()
    assert thread_b.id != thread_a.id
    await runtime.run("继续 ProjectAlpha", frozenset())
    assert repository(runtime).active().id == thread_a.id
    replay = provider.requests[-1]
    assert "预算五元" in texts(replay) and "预算十元" not in texts(replay)
    calls = [m for m in replay if getattr(m, "tool_calls", None)]
    assert calls[0].provider_data == {"opaque": [{"value": "keep"}]}
    assert calls[0].tool_calls[0].id == "source-sum"
    assert len([m for m in replay if m.role == "tool"]) == 1
    assert len(provider.requests) == 4  # The router is rule-based by default, once per Run.
    assert runtime.sessions.current.id == conversation
    with runtime.sessions.store.reading() as db:
        assert db.execute("SELECT COUNT(*) FROM run_threads").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
    await runtime.close()


async def test_thread_summaries_skip_other_runs_and_ignore_legacy_checkpoint(routed):
    runtime, provider = routed
    repo = repository(runtime)
    a, b = repo.create("Alpha"), repo.create("Beta")
    completed(repo, a, "A first")
    completed(repo, b, "B private")
    completed(repo, a, "A second")
    history = SQLiteThreadHistory(repo.store, repo.conversation_id)
    with repo.store._transaction() as db:
        whole = context.page(db, repo.conversation_id, 0)
        context.save_summary(db, repo.conversation_id, "LEGACY MIXED SUMMARY", None, whole.turns)
    history.thread_id = a.id
    page_a = history.page(0)
    assert len(page_a.turns) == 2
    assert history.compact_through(1) == page_a.turns[0].last_sequence
    a_summary = history.save_summary("Only A state", None, page_a.turns)
    history.thread_id = b.id
    assert history.summary() is None
    history.save_summary("Only B state", None, history.page(0).turns)
    history.thread_id = a.id
    assert history.summary() == a_summary
    repo.select(a.id)
    provider.responses += [ModelResponse(text="done")]
    assert (await runtime.run("continue", frozenset())).status == "completed"
    request = texts(provider.requests[-1])
    assert '"kind": "thread_working_summary"' in request and "Only A state" in request
    assert "Only B state" not in request and "LEGACY MIXED SUMMARY" not in request
    with repo.store.reading() as db:
        assert len(context.page(db, repo.conversation_id, 0).turns) == 4
    await runtime.close()


def test_restart_source_scoping_and_unbound_history_cannot_save_summary(routed):
    runtime, _ = routed
    repo = repository(runtime)
    thread = repo.create("one")
    run = completed(repo, thread, "CLI only")
    other = repository(runtime, "chat:one")
    other.select(thread.id)
    completed(other, thread, "QQ only")
    restored = SQLiteThreadHistory(repo.store, repo.conversation_id)
    restored.thread_id = thread.id
    assert restored.page(0).turns[0].run_id == run and len(restored.page(0).turns) == 1
    restored.source = "chat:one"
    assert "QQ only" in restored.page(0).messages[0].text
    assert repository(runtime).active().id == thread.id
    restored.thread_id = None
    with pytest.raises(HistoryError, match="selected"):
        restored.save_summary("bad", None, [])


async def test_router_semantic_decision_uses_empty_tools_and_shared_budget(routed):
    runtime, _ = routed
    repo = repository(runtime)
    a, b = repo.create("alpha"), repo.create("beta")
    completed(repo, b, "recent question")
    run = repo.store.begin_run(
        repo.conversation_id, TextMessage(role="user", text="long ambiguous input")
    )
    provider = FakeProvider(ModelResponse(text=json.dumps({"action": "select", "thread_id": a.id})))
    from personal_agent.core.execution import BudgetProvider

    budget = ExecutionBudget()
    router = ThreadRouter(repo, model_selection=True)
    result = await router.route(run, "long ambiguous input", BudgetProvider(provider, budget))
    assert result.id == a.id and budget.requests == 1
    assert provider.tools == [[]] and provider.output_limits == [1024]
    assert router.metrics["extra_requests"] == 1
    data = json.loads(provider.requests[0][1].text)
    assert data["active_thread_id"] == b.id and data["recent"]


@pytest.mark.parametrize("answer", ['{"action":"select","thread_id":"invented"}', "invalid"])
async def test_invalid_router_decision_keeps_current_thread_without_retry(routed, answer):
    runtime, _ = routed
    repo = repository(runtime)
    active = repo.create("alpha")
    completed(repo, active, "past")
    run = repo.store.begin_run(
        repo.conversation_id, TextMessage(role="user", text="ambiguous input")
    )
    provider = FakeProvider(ModelResponse(text=answer))
    router = ThreadRouter(repo, model_selection=True)
    assert (await router.route(run, "ambiguous input", provider)).id == active.id
    assert router.metrics["reason"] == "selection_unavailable" and len(provider.requests) == 1


async def test_router_cancellation_does_not_bind_or_execute(routed):
    runtime, _ = routed
    repo = repository(runtime)
    thread = repo.create("alpha")
    completed(repo, thread, "past")
    run = repo.store.begin_run(
        repo.conversation_id, TextMessage(role="user", text="ambiguous input")
    )

    class CancelProvider:
        async def generate(self, *args, **kwargs):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await ThreadRouter(repo, model_selection=True).route(
            run,
            "ambiguous input",
            CancelProvider(),
        )
    with repo.store.reading() as db:
        assert db.execute("SELECT 1 FROM run_threads WHERE run_id=?", (run,)).fetchone() is None


async def test_ambiguous_resume_has_no_tool_effects(routed):
    runtime, provider = routed
    repo = repository(runtime)
    a, b = repo.create("alpha"), repo.create("beta")
    completed(repo, b, "past")
    runtime.routing_model_selection = True
    provider.responses += [
        ModelResponse(text='{"action":"clarify"}'),
        ModelResponse(tool_calls=[add_call("must-deny")]),
        ModelResponse(text="你想继续哪个讨论？"),
    ]
    result = await runtime.run("继续之前那个复杂的任务", frozenset())
    assert result.status == "completed"
    assert provider.tools[1:] == [[], []]
    failure = next(m for m in provider.requests[-1] if m.role == "tool")
    assert not failure.result.ok and failure.result.error.code == "routing_ambiguous"
    assert repo.active().id == b.id != a.id
    await runtime.close()


async def test_online_ownership_forces_episode_boundaries_and_organizer_assignments(routed):
    runtime, _ = routed
    repo = repository(runtime)
    a, b = repo.create("alpha"), repo.create("beta")
    ids = [completed(repo, a, "A1"), completed(repo, b, "B1"), completed(repo, a, "A2")]
    episodes = EpisodeRepository(repo.store, repo.agent_id)
    assert (await organizer(episodes, SegmentProvider()).organize(repo.conversation_id))[
        "outcome"
    ] == "completed"
    values = episodes.list(repo.conversation_id)
    assert len(values) == 3
    assert {e.run_ids[0]: e.thread_id for e in values} == dict(
        zip(ids, [a.id, b.id, a.id], strict=True)
    )
    service = thread_service(repo.store)
    assert (await service.organize())["outcome"] == "failed"  # Fake tries merging A/B.
    assert {e.run_ids[0]: e.thread_id for e in episodes.list(repo.conversation_id)} == dict(
        zip(ids, [a.id, b.id, a.id], strict=True)
    )

    def fixed(output, data):
        assignment = data["fixed_thread_assignments"]
        output["groups"] = [
            dict(
                episode_ids=[eid for eid, owner in assignment.items() if owner == thread],
                thread_id=thread,
                title=title,
                current_state="有来源的整理状态",
                status="active",
            )
            for thread, title in [(a.id, "alpha"), (b.id, "beta")]
        ]

    service.model.provider = TopicProvider(mutate=fixed)
    assert (await service.organize())["outcome"] == "completed"
    assert repo.get(a.id).current_state == "有来源的整理状态"


def test_trusted_correction_invalidates_summaries_preserving_journal(routed):
    runtime, _ = routed
    repo = repository(runtime)
    a, b = repo.create("alpha"), repo.create("beta")
    run = completed(repo, a, "misrouted")
    completed(repo, b, "other")
    history = SQLiteThreadHistory(repo.store, repo.conversation_id)
    for thread in (a, b):
        history.thread_id = thread.id
        history.save_summary(thread.title, None, history.page(0).turns)
    with repo.store.reading() as db:
        original = list(db.execute("SELECT payload FROM messages ORDER BY sequence"))
    repo.correct(run, b.id)
    for thread in (a, b):
        history.thread_id = thread.id
        assert history.summary() is None
    history.thread_id = a.id
    assert not history.page(0).turns
    history.thread_id = b.id
    assert len(history.page(0).turns) == 2
    with repo.store.reading() as db:
        assert list(db.execute("SELECT payload FROM messages ORDER BY sequence")) == original
        assert (
            db.execute("SELECT revision FROM run_threads WHERE run_id=?", (run,)).fetchone()[0] == 2
        )
    with pytest.raises(HistoryError):
        repository(runtime, "other-source").correct(run, a.id)


async def test_legacy_bootstrap_keeps_existing_episode_thread(routed):
    runtime, _ = routed
    from tests.test_episodes import seed

    repo = repository(runtime)
    run = seed(repo.store, repo.conversation_id)
    episodes = EpisodeRepository(repo.store, repo.agent_id)
    await organizer(episodes).organize(repo.conversation_id)
    # Unlike the generic fixture, use this runtime's Agent identity.
    from personal_agent.organization.models import OrganizationPolicy
    from personal_agent.organization.provider import OrganizerModel
    from personal_agent.organization.threads import ThreadOrganizer
    from personal_agent.storage.threads import ThreadRepository

    service = ThreadOrganizer(
        ThreadRepository(repo.store, repo.agent_id),
        OrganizerModel(
            TopicProvider(),
            ContextBudget(context_window=131072),
            OrganizationPolicy(),
        ),
    )
    assert (await service.organize())["outcome"] == "completed"
    prior = episodes.list(repo.conversation_id)[0].thread_id
    repo.bootstrap()
    with repo.store.reading() as db:
        assert (
            db.execute("SELECT thread_id FROM run_threads WHERE run_id=?", (run,)).fetchone()[0]
            == prior
        )
    assert repo.active().id == prior


async def test_cli_new_and_use_select_threads_in_same_journal(routed):
    runtime, provider = routed
    service = SimpleNamespace(sessions=runtime.sessions, harness=runtime, agent=runtime.agent)
    out = []
    session_command(service, "/new", out.append)
    thread = repository(runtime).active()
    conversation = runtime.sessions.current.id
    provider.responses += [ModelResponse(text="ok")]
    await runtime.run("We are planning Alpha work", frozenset())
    assert repository(runtime).active().id == thread.id
    assert repository(runtime).get(thread.id).title == "We are planning Alpha work"
    session_command(service, "/thread new beta", out.append)
    session_command(service, "/resume " + thread.id, out.append)
    assert repository(runtime).active().id == thread.id
    assert runtime.sessions.current.id == conversation
    await runtime.close()


def test_migration_23_to_24_and_transaction_rollback(tmp_path, monkeypatch):
    from personal_agent.storage import sqlite as module

    path = tmp_path / "upgrade.db"
    with SQLiteStore(path) as store:
        migrations = dict(module.MIGRATIONS)
        monkeypatch.setattr(module, "SCHEMA_VERSION", 23)
        store.initialize()
        monkeypatch.setattr(module, "SCHEMA_VERSION", 24)
        monkeypatch.setitem(module.MIGRATIONS, 24, (*migrations[24], "INVALID SQL"))
        with pytest.raises(HistoryError):
            store.initialize()
        with store.reading() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 23
            assert (
                db.execute("SELECT 1 FROM sqlite_master WHERE name='run_threads'").fetchone()
                is None
            )
        monkeypatch.setitem(module.MIGRATIONS, 24, migrations[24])
        store.initialize()
        with store.reading() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 24
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []


async def test_correction_detaches_mixed_episode_and_rejects_stale_summary(routed):
    runtime, _ = routed
    repo = repository(runtime)
    a, b = repo.create("alpha"), repo.create("beta")
    completed(repo, a, "A first")
    changed = completed(repo, a, "A second")
    episodes = EpisodeRepository(repo.store, repo.agent_id)
    await organizer(episodes).organize(repo.conversation_id)
    original = episodes.list(repo.conversation_id)
    assert len(original) == 1 and original[0].thread_id == a.id
    history = SQLiteThreadHistory(repo.store, repo.conversation_id)
    history.thread_id = a.id
    stale = history.page(0).turns
    repo.correct(changed, b.id)
    detached = episodes.list(repo.conversation_id)[0]
    assert detached.id == original[0].id and detached.thread_id is None
    assert not detached.thread_dirty and repo.get(a.id).dirty
    with pytest.raises(HistoryError, match="source changed"):
        history.save_summary("stale mixed state", None, stale)
