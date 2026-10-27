"""Phase 1 contracts; fake decisions test host control, not LLM semantic quality."""

import asyncio
import json
from pathlib import Path

import pytest

from personal_agent.automation.models import AutomationPolicy
from personal_agent.automation.service import MemoryAutomation
from personal_agent.core.agent import Agent
from personal_agent.core.budget import ContextBudget
from personal_agent.core.history import HistoryError
from personal_agent.core.models import ModelResponse, TokenUsage
from personal_agent.main import chat
from personal_agent.memory.models import digest
from personal_agent.memory.store import MemoryFS
from personal_agent.reflection.service import ReflectionService
from personal_agent.sessions import SessionManager
from personal_agent.storage.reflection import ReflectionRepository
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.tools.registry import ToolRegistry
from tests.fakes import FakeProvider
from tests.test_reflection import FACT, TOPIC, candidate, decision, scripted, seed

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def gate(value=True):
    return ModelResponse(
        text=json.dumps(
            dict(
                should_reflect=value,
                reason="synthetic decision",
                category="preference" if value else "none",
                confidence="high",
            )
        ),
        usage=TokenUsage(input_tokens=100, output_tokens=50, reasoning_tokens=10),
    )


@pytest.fixture
def state(tmp_path):
    with SQLiteStore(tmp_path / "automation.sqlite3") as db:
        db.initialize()
        sessions = SessionManager(db, budget=ContextBudget(context_window=64000))
        memory = MemoryFS.for_agent(tmp_path / "memory", "heybuddy")
        memory.initialize()
        sessions.memory = memory
        yield db, sessions, memory


def automation(state, provider, **policy):
    db, _, memory = state
    reflection = ReflectionService(
        ReflectionRepository(db, "heybuddy"), memory, provider, ContextBudget(context_window=64000)
    )
    return MemoryAutomation(reflection, AutomationPolicy(**policy))


def plan_provider(state, run, *, changes=None, fact=None):
    _, sessions, _ = state
    provider = scripted([fact or candidate(sessions.current.id, run)], changes or [decision()])
    provider.responses.insert(0, gate())
    return provider


async def test_suggest_proposes_and_requires_original_approval(state):
    db, sessions, memory = state
    run = seed(db, sessions.current.id)
    provider = plan_provider(state, run)
    service = automation(state, provider)
    result = await service.run(sessions.current.id)
    assert result["outcome"] == "plan_created" and result["metrics"]["requests"] == 4
    assert result["candidates"] == 1 and memory.read().blocks["human.md"] == "# Human\n"
    assert service.status(sessions.current.id)["reviewed_through"] == 0
    assert (await service.run(sessions.current.id))["outcome"] == "pending_review"
    assert len(provider.requests) == 4
    plan = service.reflection.apply(sessions.current.id, result["plan_id"], 1)
    assert plan.closed and "回答先给结论" in memory.read().blocks["human.md"]
    sessions.new()
    assert (
        "回答先给结论" in sessions.context().for_request(sessions.context().build("偏好"))[1].text
    )


@pytest.mark.parametrize(
    "text", ["什么是 Python 列表？", "假设我在北京，怎么安排旅行？", "17+25是多少？"]
)
async def test_negative_worthiness_is_persistent_and_skips_full_reflection(state, text):
    db, sessions, memory = state
    seed(db, sessions.current.id, text)
    provider = FakeProvider(gate(False))
    service = automation(state, provider)
    result = await service.run(sessions.current.id)
    assert result["outcome"] == "not_worthy" and result["metrics"]["requests"] == 1
    assert service.status(sessions.current.id)["reviewed_through"] == 2
    assert await service.run(sessions.current.id) == {"outcome": "no_history", "requests": 0}
    assert len(provider.requests) == 1 and memory.read().blocks["human.md"] == "# Human\n"
    row = db._connection.execute("SELECT decision,plan_id FROM automation_batches").fetchone()
    assert json.loads(row[0])["should_reflect"] is False and row[1] is None


async def test_pending_plan_keeps_new_history_and_rejection_allows_next_batch(state):
    db, sessions, _ = state
    run = seed(db, sessions.current.id)
    provider = plan_provider(state, run)
    service = automation(state, provider)
    first = await service.run(sessions.current.id)
    seed(db, sessions.current.id, "这是新的临时问题。")
    paused = await service.run(sessions.current.id)
    assert paused["outcome"] == "pending_review" and paused["unprocessed_runs"] == 1
    assert paused["reviewed_through"] == 0 and len(provider.requests) == 4
    service.reflection.decide(sessions.current.id, first["plan_id"], None, "rejected")
    provider.responses.append(gate(False))
    assert (await service.run(sessions.current.id))["outcome"] == "not_worthy"
    assert service.status(sessions.current.id)["reviewed_through"] == 4
    assert len(db.load_messages(sessions.current.id)) == 4


@pytest.mark.parametrize(
    "mode,completed,expected", [("off", True, "off"), ("suggest", False, "ineligible")]
)
async def test_cheap_eligibility_never_calls_provider(state, mode, completed, expected):
    provider = FakeProvider()
    service = automation(state, provider, mode=mode)
    assert (await service.run(state[1].current.id, completed=completed))["outcome"] == expected
    assert provider.requests == []


@pytest.mark.parametrize(
    "response",
    [
        ModelResponse(text="not JSON"),
        ModelResponse(text='{"should_reflect":true,"approved":true}'),
        RuntimeError("private provider body"),
    ],
)
async def test_failure_blocks_automatic_repetition_but_explicit_retry_works(state, response):
    db, sessions, _ = state
    seed(db, sessions.current.id)
    provider = FakeProvider(response, gate(False))
    service = automation(state, provider)
    first = await service.run(sessions.current.id)
    assert (
        first["outcome"] == "failed"
        and service.status(sessions.current.id)["reviewed_through"] == 0
    )
    assert (await service.run(sessions.current.id))["outcome"] == "blocked"
    assert len(provider.requests) == 1
    retry = await service.run(sessions.current.id, retry=True)
    assert retry["outcome"] == "not_worthy" and retry["metrics"]["requests"] == 2


async def test_budget_and_reflection_failure_preserve_successful_worthiness(state):
    db, sessions, _ = state
    run = seed(db, sessions.current.id)
    provider = plan_provider(state, run)
    service = automation(state, provider, max_requests=1)
    result = await service.run(sessions.current.id)
    assert result["outcome"] == "failed" and result["error_code"] == "automation_call_budget"
    assert len(provider.requests) == 1
    service.policy = AutomationPolicy()
    result = await service.run(sessions.current.id, retry=True)
    assert result["outcome"] == "plan_created" and result["metrics"]["requests"] == 4
    assert len(provider.requests) == 4  # The stored positive decision was not called again.


async def test_answer_is_displayed_before_automation_and_failure_does_not_change_run(state):
    db, sessions, _ = state
    provider = FakeProvider(ModelResponse(text="普通回答已完成。"), RuntimeError("private failure"))
    service = automation(state, provider)
    output = []
    inputs = iter(["什么是 Python 列表？", "quit"])
    agent = Agent(provider, ToolRegistry(), context=sessions.context())
    await chat(
        agent,
        read=lambda _: next(inputs),
        write=output.append,
        sessions=sessions,
        reflection=service.reflection,
        automation=service,
    )
    assert next(i for i, line in enumerate(output) if line.startswith("Agent >")) < next(
        i for i, line in enumerate(output) if line.startswith("Memory >")
    )
    assert db.list_runs(sessions.current.id)[0].status == "completed"
    assert "private failure" not in "\n".join(output)
    assert len(provider.requests) == 2


async def test_auto_only_accepts_exact_host_create_grant_and_never_core_or_correction(state):
    db, sessions, memory = state
    run = seed(db, sessions.current.id)
    content = TOPIC.replace("heybuddy", "indexed-learning")
    path = "knowledge/indexed-learning.md"
    fact = candidate(sessions.current.id, run, category="knowledge")
    provider = plan_provider(
        state, run, fact=fact, changes=[decision("create", path=path, new_text=content)]
    )
    service = automation(
        state, provider, mode="auto", auto_create_grants={path: digest(content.encode())}
    )
    result = await service.run(sessions.current.id)
    assert result["auto_attempted"] == [1] and memory.path(path).exists()
    assert service.reflection.review(sessions.current.id) is None
    assert service.status(sessions.current.id)["reviewed_through"] == 2
    assert len(db.load_messages(sessions.current.id)) == 2


@pytest.mark.parametrize("category", ["knowledge", "correction", "preference"])
async def test_ungranted_or_ineligible_auto_proposals_remain_pending(state, category, grant=None):
    db, sessions, memory = state
    run = seed(db, sessions.current.id)
    content = TOPIC.replace("heybuddy", "bounded-learning")
    path = "knowledge/bounded-learning.md"
    provider = plan_provider(
        state,
        run,
        fact=candidate(sessions.current.id, run, category=category),
        changes=[decision("create", path=path, new_text=content)],
    )
    grants = {} if category == "knowledge" else {path: digest(content.encode())}
    service = automation(state, provider, mode="auto", auto_create_grants=grants)
    result = await service.run(sessions.current.id)
    assert result["auto_attempted"] == [] and not memory.path(path).exists()
    assert service.reflection.review(sessions.current.id).items[0].state == "pending"


async def test_gate_negative_progress_is_atomic_on_database_failure(state):
    db, sessions, _ = state
    seed(db, sessions.current.id)
    db._connection.execute(
        "CREATE TRIGGER reject_progress BEFORE INSERT ON reflection_progress "
        "BEGIN SELECT RAISE(ABORT,'fail'); END"
    )
    service = automation(state, FakeProvider(gate(False)))
    assert (await service.run(sessions.current.id))["outcome"] == "failed"
    assert service.status(sessions.current.id)["reviewed_through"] == 0
    row = db._connection.execute("SELECT state,decision FROM automation_batches").fetchone()
    assert row[0] == "failed" and row[1] is None


async def test_plan_saved_before_ledger_failure_is_recovered_without_model_retry(
    state, monkeypatch
):
    db, sessions, _ = state
    run = seed(db, sessions.current.id)
    service = automation(state, plan_provider(state, run))
    original = service.repository.update

    def fail(batch, **kwargs):
        if kwargs["state"] == "plan_created":
            raise HistoryError("synthetic ledger failure")
        return original(batch, **kwargs)

    monkeypatch.setattr(service.repository, "update", fail)
    assert (await service.run(sessions.current.id))["outcome"] == "failed"
    assert service.reflection.review(sessions.current.id)
    monkeypatch.setattr(service.repository, "update", original)
    assert (await service.run(sessions.current.id))["outcome"] == "pending_review"
    assert len(service.reflection.provider.requests) == 4


async def test_cancelled_gate_preserves_completed_run_and_needs_explicit_retry(state):
    db, sessions, _ = state
    seed(db, sessions.current.id)
    started = asyncio.Event()

    class Slow:
        async def generate(self, *args, **kwargs):
            started.set()
            await asyncio.Event().wait()

    service = automation(state, Slow())
    task = asyncio.create_task(service.run(sessions.current.id))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert db.list_runs(sessions.current.id)[0].status == "completed"
    assert (await service.run(sessions.current.id))["outcome"] == "blocked"
    assert service.status(sessions.current.id)["latest"]["state"] == "interrupted"


async def test_conversation_agent_isolation_and_metrics_are_separate(state):
    db, sessions, _ = state
    seed(db, sessions.current.id)
    service = automation(state, FakeProvider(gate(False)))
    await service.run(sessions.current.id)
    previous = sessions.current.id
    sessions.new()
    assert service.status(sessions.current.id)["decisions"] == 0
    assert service.status(previous)["latest"]["metrics"]["reasoning_tokens"] == 10
    foreign = db.create_agent("foreign", "base", agent_id="foreign")
    conversation = db.create_conversation(foreign.id)
    with pytest.raises(HistoryError):
        service.status(conversation.id)


def test_schema_four_to_five_is_atomic(tmp_path, monkeypatch):
    import sqlite3

    from personal_agent.storage import sqlite as storage

    path = tmp_path / "v4.sqlite3"
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 4)
        with SQLiteStore(path) as db:
            db.initialize()
            sessions = SessionManager(db)
            seed(db, sessions.current.id)
    with monkeypatch.context() as patch:
        patch.setitem(storage.MIGRATIONS, 5, (*storage.MIGRATIONS[5], "INVALID SQL"))
        with SQLiteStore(path) as db:
            with pytest.raises(HistoryError):
                db.initialize()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='automation_batches'"
        ).fetchone()
    with SQLiteStore(path) as db:
        db.initialize()
        assert len(db.load_messages(sessions.current.id)) == 2


async def test_project_correction_and_duplicate_use_existing_reflection_contracts(state):
    from personal_agent.memory import index

    db, sessions, memory = state
    index.create(memory, "projects/heybuddy.md", TOPIC + "\n保留无关架构。\n")
    text = "旧方案 A 已废弃，现在正式采用方案 B。"
    run = seed(db, sessions.current.id, text)
    fact = candidate(sessions.current.id, run, quote=text, category="correction")
    provider = scripted(
        [fact],
        [decision(path="projects/heybuddy.md", old_text="采用方案 A。", new_text="采用方案 B。")],
        paths=["projects/heybuddy.md"],
    )
    provider.responses.insert(0, gate())
    service = automation(state, provider)
    result = await service.run(sessions.current.id)
    plan = service.reflection.review(sessions.current.id)
    assert plan.items[0].decision.path == "projects/heybuddy.md"
    assert "方案 A" in memory.read_file("projects/heybuddy.md")["content"]
    service.reflection.apply(sessions.current.id, result["plan_id"], None)
    assert "保留无关架构" in memory.read_file("projects/heybuddy.md")["content"]
    assert "方案 B" in memory.read_file("projects/heybuddy.md")["content"]
    repeated = seed(db, sessions.current.id, FACT)
    duplicate = scripted(
        [candidate(sessions.current.id, repeated, sequence=3)], [decision("no_op")]
    )
    duplicate.responses.insert(0, gate())
    other = automation(state, duplicate)
    result = await other.run(sessions.current.id)
    assert other.reflection.review(sessions.current.id).items[0].decision.action == "no_op"


async def test_restart_after_positive_decision_does_not_repeat_gate_automatically(tmp_path):
    import os
    import subprocess
    import sys

    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("OPENAI_", "AGENT_"))
    }
    env.update(
        PYTHONPATH=os.pathsep.join([str(PROJECT_ROOT), str(PROJECT_ROOT / "src")]), PYTHONUTF8="1"
    )
    bootstrap = r"""
import asyncio, os
from pathlib import Path
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.sessions import SessionManager
from personal_agent.memory.store import MemoryFS
from personal_agent.automation.service import MemoryAutomation, WORTHINESS_PROMPT
from tests.test_automation import automation, gate
from tests.test_reflection import seed
class Crash:
    async def generate(self,messages,tools,**kwargs):
        if messages[0].text==WORTHINESS_PROMPT:
            return gate()
        os._exit(79)
with SQLiteStore(Path('restart.sqlite3')) as db:
    db.initialize()
    sessions=SessionManager(db)
    memory=MemoryFS.for_agent(Path('memory'),'heybuddy')
    memory.initialize()
    seed(db,sessions.current.id)
    service=automation((db,sessions,memory),Crash())
    asyncio.run(service.run(sessions.current.id))
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-X", "utf8", "-c", bootstrap],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 79, (result.stderr, result.stdout)
    with SQLiteStore(tmp_path / "restart.sqlite3") as db:
        db.initialize()
        sessions = SessionManager(db)
        memory = MemoryFS.for_agent(tmp_path / "memory", "heybuddy")
        memory.initialize()
        run = db.list_runs(sessions.current.id)[0].id
        provider = scripted([candidate(sessions.current.id, run)], [decision()])
        service = automation((db, sessions, memory), provider)
        service.repository.recover()
        assert (await service.run(sessions.current.id))["outcome"] == "blocked"
        assert provider.requests == []
        resumed = await service.run(sessions.current.id, retry=True)
        assert resumed["outcome"] == "plan_created" and len(provider.requests) == 3
        assert len(db.load_messages(sessions.current.id)) == 2


async def test_pending_auto_plan_and_memory_grants_survive_reopen_without_repeated_write(state):
    db, sessions, memory = state
    run = seed(db, sessions.current.id)
    provider = plan_provider(state, run)
    service = automation(state, provider)
    result = await service.run(sessions.current.id)
    fresh = automation(state, FakeProvider())
    assert (await fresh.run(sessions.current.id))["outcome"] == "pending_review"
    assert fresh.reflection.review(sessions.current.id).id == result["plan_id"]
    assert memory.read().blocks["human.md"] == "# Human\n"
