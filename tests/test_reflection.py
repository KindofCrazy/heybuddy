"""Offline evidence-to-proposal-to-approved-Memory contracts and crash windows."""

import asyncio
import json
from pathlib import Path

import pytest

from personal_agent.core.agent import Agent
from personal_agent.core.budget import ContextBudget, estimate_request
from personal_agent.core.history import HistoryError
from personal_agent.core.memory import CoreMemoryError
from personal_agent.core.models import (
    AssistantMessage,
    ModelResponse,
    RunResult,
    TextMessage,
    TokenUsage,
)
from personal_agent.llm.base import ProviderError
from personal_agent.main import chat
from personal_agent.memory import index
from personal_agent.memory.store import MemoryFS
from personal_agent.reflection.cli import render
from personal_agent.reflection.models import ReflectionError, ReflectionPolicy
from personal_agent.reflection.service import CONSOLIDATE, EXTRACT, SELECT, ReflectionService
from personal_agent.sessions import SessionManager
from personal_agent.storage.reflection import ReflectionRepository
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.tools.registry import ToolRegistry
from tests.fakes import FakeProvider, add_call

PROJECT_ROOT = Path(__file__).resolve().parents[1]

FACT = "请长期记住：回答要先给结论，保持简洁。"
TOPIC = "---\nname: heybuddy\ndescription: HeyBuddy 架构决策\n---\n\n# HeyBuddy\n\n采用方案 A。\n"


def seed(db, conversation, text=FACT, *, status="completed"):
    run = db.begin_run(conversation, TextMessage(role="user", text=text))
    db.append_message(run, AssistantMessage(text="已了解。"))
    if status in {"completed", "failed"}:
        db.finish_run(
            run,
            RunResult(
                status="completed" if status == "completed" else "provider_error",
                text="done",
                rounds=1,
            ),
        )
    else:
        db.interrupt_run(run, cancelled=status == "cancelled")
    return run


def candidate(
    conversation,
    run,
    *,
    sequence=1,
    quote=FACT,
    confidence="confirmed",
    category="preference",
    content="回答先给结论，保持简洁。",
    subject="回复偏好",
):
    return dict(
        category=category,
        subject=subject,
        content=content,
        reason="稳定且明确的长期偏好。",
        confidence=confidence,
        evidence=[dict(conversation_id=conversation, run_id=run, sequence=sequence, quote=quote)],
    )


def decision(action="update", **changes):
    result = dict(candidate_id=1, action=action, reason="整合已有原则，不追加零散规则。")
    if action == "update":
        result.update(
            path="human.md", old_text="# Human\n", new_text="# Human\n\n回答先给结论，保持简洁。\n"
        )
    return result | changes


def scripted(candidates, decisions, paths=()):
    return FakeProvider(
        ModelResponse(text=json.dumps({"candidates": candidates}, ensure_ascii=False)),
        ModelResponse(text=json.dumps({"paths": list(paths)})),
        ModelResponse(text=json.dumps({"decisions": decisions}, ensure_ascii=False)),
    )


def service(db, memory, provider, **policy):
    return ReflectionService(
        ReflectionRepository(db, "heybuddy"),
        memory,
        provider,
        ContextBudget(context_window=64000),
        ReflectionPolicy(**policy),
    )


@pytest.fixture
def state(tmp_path):
    with SQLiteStore(tmp_path / "reflection.sqlite3") as db:
        db.initialize()
        sessions = SessionManager(db, budget=ContextBudget(context_window=64000))
        memory = MemoryFS.for_agent(tmp_path / "memory", "heybuddy")
        memory.initialize()
        sessions.memory = memory
        run = seed(db, sessions.current.id)
        yield db, sessions, memory, run


async def proposal(state, *, changes=None, candidates=None, paths=()):
    db, sessions, memory, run = state
    provider = scripted(
        candidates or [candidate(sessions.current.id, run)], changes or [decision()], paths
    )
    reflection = service(db, memory, provider)
    return reflection, await reflection.reflect(sessions.current.id), provider


async def test_preference_evidence_approval_and_future_context(state):
    db, sessions, memory, run = state
    initial = memory.read()
    reflection, plan, provider = await proposal(state)
    assert memory.read() == initial
    assert reflection.status(sessions.current.id)["through_sequence"] == 0
    assert [request[0].text for request in provider.requests] == [EXTRACT, SELECT, CONSOLIDATE]
    assert all(tools == [] for tools in provider.tools)
    assert all(limit == 4096 for limit in provider.output_limits)
    assert all(estimate_request(request, []) <= 16384 for request in provider.requests)
    assert plan.items[0].candidate.evidence[0].run_id == run
    assert "before span" in render(plan) and FACT in render(plan)
    assert (await reflection.reflect(sessions.current.id)).id == plan.id
    assert len(provider.requests) == 3
    finished = reflection.apply(sessions.current.id, plan.id, 1)
    assert finished.closed and finished.items[0].state == "applied"
    assert finished.items[0].approved_at is not None
    assert reflection.status(sessions.current.id)["through_sequence"] == 2
    with pytest.raises(ReflectionError):
        reflection.apply(sessions.current.id, plan.id, 1)
    sessions.new()
    context = sessions.context()
    request = context.for_request(context.build("偏好是什么？"))
    assert "回答先给结论" in request[1].text
    assert reflection.status(sessions.current.id)["through_sequence"] == 0
    assert len(db.load_messages(plan.conversation_id)) == 2


async def test_project_creation_is_indexed_only_after_approval(state):
    db, sessions, memory, run = state
    reflection, plan, _ = await proposal(
        state,
        changes=[
            decision(
                "create",
                path="projects/heybuddy.md",
                new_text=TOPIC.replace("方案 A", "自建 Harness"),
            )
        ],
    )
    assert not memory.path("projects/heybuddy.md").exists()
    result = reflection.apply(sessions.current.id, plan.id, None)
    assert result.closed
    assert index.inspect(memory)["ok"]
    assert "projects/MEMORY.md" in memory.read().blocks["MEMORY.md"]
    assert "projects/heybuddy.md" in memory.read_file("projects/MEMORY.md")["content"]
    assert "自建 Harness" in memory.read_file("projects/heybuddy.md")["content"]


async def test_correction_replaces_old_decision_and_preserves_unrelated_content(state):
    db, sessions, memory, run = state
    index.create(memory, "projects/heybuddy.md", TOPIC + "\n保留其他信息。\n")
    correction = "明确更正：不用方案 A，改用方案 B。"
    newer = seed(db, sessions.current.id, correction)
    current = candidate(
        sessions.current.id,
        newer,
        sequence=3,
        quote=correction,
        category="correction",
        content="当前采用方案 B。",
    )
    reflection, plan, _ = await proposal(
        state,
        candidates=[current],
        paths=["projects/heybuddy.md"],
        changes=[
            decision(path="projects/heybuddy.md", old_text="采用方案 A。", new_text="采用方案 B。")
        ],
    )
    reflection.apply(sessions.current.id, plan.id, 1)
    text = memory.read_file("projects/heybuddy.md")["content"]
    assert "采用方案 A" not in text and "采用方案 B" in text and "保留其他信息" in text


@pytest.mark.parametrize("action", ["no_op", "reject"])
async def test_noop_and_reject_are_reviewed_without_writing(state, action):
    _, sessions, memory, _ = state
    before = memory.read()
    reflection, plan, _ = await proposal(state, changes=[decision(action)])
    assert not plan.closed
    result = reflection.apply(sessions.current.id, plan.id, None)
    assert result.closed and memory.read() == before
    assert await reflection.reflect(sessions.current.id) is None


async def test_duplicate_topic_requires_review_instead_of_second_file(state):
    _, sessions, memory, _ = state
    index.create(memory, "projects/heybuddy.md", TOPIC)
    reflection, plan, _ = await proposal(
        state, changes=[decision("create", path="other/heybuddy.md", new_text=TOPIC)]
    )
    assert plan.items[0].decision.action == "review"
    result = reflection.apply(sessions.current.id, plan.id, None)
    assert not result.closed and not memory.path("other/heybuddy.md").exists()
    assert reflection.decide(sessions.current.id, plan.id, None, "skipped").closed


async def test_uncertain_candidate_cannot_be_applied(state):
    _, sessions, memory, run = state
    fact = candidate(sessions.current.id, run, confidence="uncertain")
    reflection, plan, _ = await proposal(state, candidates=[fact])
    assert plan.items[0].decision.action == "review"
    result = reflection.apply(sessions.current.id, plan.id, None)
    assert result.items[0].state == "pending" and memory.read().blocks["human.md"] == "# Human\n"


async def test_rejection_skipping_empty_results_and_incremental_pages(state):
    db, sessions, memory, run = state
    reflection, plan, provider = await proposal(state)
    assert reflection.decide(sessions.current.id, plan.id, 1, "rejected").closed
    assert await reflection.reflect(sessions.current.id) is None
    assert len(provider.requests) == 3
    seed(db, sessions.current.id, "这是没有长期价值的临时交流。")
    second = service(db, memory, FakeProvider(ModelResponse(text='{"candidates":[]}')), page_runs=1)
    empty = await second.reflect(sessions.current.id)
    assert empty.closed and empty.items == []
    assert empty.after_sequence == 2 and empty.through_sequence == 4
    assert await second.reflect(sessions.current.id) is None
    seed(db, sessions.current.id)
    third = service(
        db,
        memory,
        scripted(
            [candidate(sessions.current.id, db.list_runs(sessions.current.id)[-1].id, sequence=5)],
            [decision()],
        ),
    )
    pending = await third.reflect(sessions.current.id)
    assert third.decide(sessions.current.id, pending.id, None, "skipped").closed
    assert third.status(sessions.current.id)["through_sequence"] == 6


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "invented"),
        ("conversation_id", "other"),
        ("sequence", 999),
        ("quote", "从未说过这句话"),
    ],
)
async def test_forged_evidence_rejected_without_any_memory_write(state, field, value):
    db, sessions, memory, run = state
    fact = candidate(sessions.current.id, run)
    fact["evidence"][0][field] = value
    reflection = service(db, memory, scripted([fact], [decision()]))
    with pytest.raises(ReflectionError, match="invalid_evidence"):
        await reflection.reflect(sessions.current.id)
    assert reflection.status(sessions.current.id)["pending_plan"] is None
    assert memory.read().blocks["human.md"] == "# Human\n"


async def test_assistant_only_confirmation_is_not_user_evidence(state):
    db, sessions, memory, run = state
    fact = candidate(sessions.current.id, run, sequence=2, quote="已了解。")
    reflection = service(db, memory, scripted([fact], [decision()]))
    with pytest.raises(ReflectionError, match="unconfirmed_evidence"):
        await reflection.reflect(sessions.current.id)


async def test_only_completed_sources_and_compaction_boundary_is_independent(state):
    db, sessions, memory, run = state
    for status in ("failed", "interrupted", "cancelled"):
        seed(db, sessions.current.id, "不能作为成功操作。", status=status)
    from personal_agent.storage.history import SQLiteConversationHistory

    history = SQLiteConversationHistory(db, sessions.current.id)
    page = history.page(0, max_runs=1)
    history.save_summary("Lossy summary, never source evidence.", None, page.turns)
    reflection, plan, provider = await proposal(state)
    assert len(plan.sources) == 1 and plan.sources[0].run_id == run
    assert "Lossy summary" not in provider.requests[0][1].text
    assert plan.after_sequence == 0
    assert len(db.load_messages(sessions.current.id)) == 8


@pytest.mark.parametrize(
    "response",
    [
        ModelResponse(text="not JSON"),
        ModelResponse(text='{"candidates":[]} trailing'),
        ModelResponse(text="x" * 5000),
        ModelResponse(text='{"candidates":[]}', tool_calls=[add_call()]),
        ModelResponse(
            text='{"candidates":[]}', usage=TokenUsage(input_tokens=999999, output_tokens=1)
        ),
        ModelResponse(
            text='{"candidates":[]}', usage=TokenUsage(input_tokens=1, output_tokens=999999)
        ),
        ProviderError("private API body must not leak"),
        ModelResponse(text='{"candidates":[],"password":"private-secret"}'),
    ],
)
async def test_invalid_provider_output_never_advances_or_writes(state, response):
    db, sessions, memory, _ = state
    reflection = service(db, memory, FakeProvider(response))
    with pytest.raises(ReflectionError) as error:
        await reflection.reflect(sessions.current.id)
    assert "private" not in str(error.value)
    assert reflection.status(sessions.current.id) == {
        "through_sequence": 0,
        "pending_plan": None,
        "states": {},
    }
    assert memory.read().blocks["human.md"] == "# Human\n"


async def test_timeout_and_cancellation_preserve_unreviewed_history(state):
    db, sessions, memory, _ = state
    entered = asyncio.Event()

    class Slow:
        async def generate(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    reflection = service(db, memory, Slow(), timeout_seconds=0.01)
    with pytest.raises(ReflectionError, match="provider_error"):
        await reflection.reflect(sessions.current.id)
    reflection = service(db, memory, Slow())
    task = asyncio.create_task(reflection.reflect(sessions.current.id))
    await entered.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reflection.repository.progress(sessions.current.id) == 0
    assert reflection.review(sessions.current.id) is None


async def test_external_editor_change_and_missing_target_are_conflicts(state):
    _, sessions, memory, _ = state
    reflection, plan, _ = await proposal(state)
    memory.path("human.md").write_text("# Human\n人工修改\n", encoding="utf-8")
    result = reflection.apply(sessions.current.id, plan.id, None)
    assert result.items[0].state == "conflict"
    assert result.items[0].approved_at is not None
    assert "人工修改" in memory.read().blocks["human.md"]
    assert not result.closed
    assert reflection.apply(sessions.current.id, plan.id, None).items[0].state == "conflict"


async def test_plan_insert_failure_and_approval_failure_have_no_memory_effect(state):
    db, sessions, memory, _ = state
    db._connection.execute(
        "CREATE TRIGGER reject_plan BEFORE INSERT ON reflection_plans "
        "BEGIN SELECT RAISE(ABORT,'fail'); END"
    )
    with pytest.raises(HistoryError):
        await proposal(state)
    assert memory.read().blocks["human.md"] == "# Human\n"
    db._connection.execute("DROP TRIGGER reject_plan")
    reflection, plan, _ = await proposal(state)
    db._connection.execute(
        "CREATE TRIGGER reject_approval BEFORE UPDATE ON reflection_plans "
        "BEGIN SELECT RAISE(ABORT,'fail'); END"
    )
    with pytest.raises(HistoryError):
        reflection.apply(sessions.current.id, plan.id, None)
    assert memory.read().blocks["human.md"] == "# Human\n"
    assert reflection.review(sessions.current.id).items[0].state == "pending"


async def test_file_saved_then_sqlite_failure_requires_reconcile_not_rewrite(state, monkeypatch):
    db, sessions, memory, _ = state
    reflection, plan, _ = await proposal(state)
    db._connection.execute("""CREATE TRIGGER reject_result BEFORE UPDATE ON reflection_plans
        WHEN json_extract(NEW.payload,'$.items[0].state')='applied'
        BEGIN SELECT RAISE(ABORT,'fail'); END""")
    with pytest.raises(HistoryError):
        reflection.apply(sessions.current.id, plan.id, None)
    assert "回答先给结论" in memory.read().blocks["human.md"]
    assert reflection.review(sessions.current.id).items[0].state == "applying"
    db._connection.execute("DROP TRIGGER reject_result")

    def forbidden(*args, **kwargs):
        raise AssertionError("Already successful writes must not run again")

    monkeypatch.setattr(memory, "edit", forbidden)
    assert reflection.apply(sessions.current.id, plan.id, None).items[0].state == "applying"
    verified = reflection.reconcile(sessions.current.id, plan.id, 1)
    assert verified.closed and verified.items[0].result_code == "verified_saved"


async def test_index_partial_failure_is_saved_and_explicitly_reconciled(state, monkeypatch):
    _, sessions, memory, _ = state
    reflection, plan, _ = await proposal(
        state, changes=[decision("create", path="projects/heybuddy.md", new_text=TOPIC)]
    )
    original = index.ensure_chain

    def fail(*args):
        from personal_agent.core.memory import CoreMemoryError

        raise CoreMemoryError("test_index_failure", "synthetic failure")

    monkeypatch.setattr(index, "ensure_chain", fail)
    result = reflection.apply(sessions.current.id, plan.id, None)
    assert result.items[0].state == "partial" and result.items[0].saved_hash
    assert memory.path("projects/heybuddy.md").exists()
    assert not reflection.reconcile(sessions.current.id, plan.id, 1).closed
    monkeypatch.setattr(index, "ensure_chain", original)
    assert index.repair(memory)["ok"]
    assert reflection.reconcile(sessions.current.id, plan.id, 1).closed


async def test_multiple_items_preserve_success_and_leave_conflicting_item(state):
    db, sessions, memory, run = state
    facts = [
        candidate(sessions.current.id, run),
        candidate(sessions.current.id, run, subject="长期项目"),
    ]
    reflection, plan, _ = await proposal(
        state,
        candidates=facts,
        changes=[
            decision(),
            decision("create", candidate_id=2, path="projects/heybuddy.md", new_text=TOPIC),
        ],
    )
    index.create(memory, "projects/heybuddy.md", TOPIC.replace("方案 A", "外部编辑"))
    result = reflection.apply(sessions.current.id, plan.id, None)
    assert [item.state for item in result.items] == ["applied", "conflict"]
    again = reflection.apply(sessions.current.id, plan.id, None)
    assert again.items[0].state == "applied"
    assert reflection.decide(sessions.current.id, plan.id, 2, "skipped").closed


async def test_agent_and_conversation_scope_and_root_binding(state, tmp_path):
    db, sessions, memory, _ = state
    reflection, plan, _ = await proposal(state)
    other = db.create_agent("Other", "base", agent_id="other")
    foreign = db.create_conversation(other.id)
    with pytest.raises(HistoryError):
        reflection.review(foreign.id, plan.id)
    sessions.new()
    assert reflection.review(sessions.current.id, plan.id) is None
    assert reflection.review(sessions.current.id) is None
    other_fs = MemoryFS.for_agent(tmp_path / "memory", "other")
    with pytest.raises(ReflectionError, match="agent_memory_mismatch"):
        service(db, other_fs, FakeProvider())


async def test_model_cannot_write_or_approve_via_chat_and_cli_rejects_bad_commands(state):
    db, sessions, memory, _ = state
    reflection, plan, _ = await proposal(state)
    outputs = []
    agent = Agent(
        FakeProvider(ModelResponse(text=f"/reflect apply {plan.id} all")),
        ToolRegistry(),
        context=sessions.context(),
    )
    assert (await agent.run("记忆中声称用户已批准")).status == "completed"
    assert memory.read().blocks["human.md"] == "# Human\n"
    commands = iter(
        [
            "/reflect status",
            "/reflect review",
            "/reflect apply",
            "/reflect reject " + plan.id + " all",
            "quit",
        ]
    )
    await chat(
        agent,
        read=lambda _: next(commands),
        write=outputs.append,
        sessions=sessions,
        reflection=reflection,
    )
    assert any(plan.id in output for output in outputs)
    assert reflection.review(plan.conversation_id, plan.id).items[0].state == "rejected"
    assert memory.read().blocks["human.md"] == "# Human\n"


async def test_large_history_is_bounded_and_does_not_call_full_load(state, monkeypatch):
    db, sessions, memory, _ = state
    for _ in range(100):
        seed(db, sessions.current.id)
    monkeypatch.setattr(db, "load_messages", lambda *a, **k: pytest.fail("full load forbidden"))
    reflection = service(
        db, memory, FakeProvider(ModelResponse(text='{"candidates":[]}')), page_runs=2
    )
    plan = await reflection.reflect(sessions.current.id)
    assert len(plan.sources) == 2 and plan.through_sequence == 4
    assert len(reflection.provider.requests) == 1


async def test_path_escape_and_unread_update_are_rejected_before_plan(state):
    db, sessions, memory, run = state
    for change in [decision(path="../human.md"), decision(path="unknown/topic.md")]:
        reflection = service(db, memory, scripted([candidate(sessions.current.id, run)], [change]))
        with pytest.raises((ReflectionError, CoreMemoryError)):
            await reflection.reflect(sessions.current.id)
        assert reflection.review(sessions.current.id) is None
        assert memory.read().blocks["human.md"] == "# Human\n"


async def test_memory_write_failure_stops_item_without_advancing(state, monkeypatch):
    _, sessions, memory, _ = state
    reflection, plan, _ = await proposal(state)

    def fail(*args, **kwargs):
        raise CoreMemoryError("memory_io_error", "synthetic write failure")

    monkeypatch.setattr(memory, "edit", fail)
    result = reflection.apply(sessions.current.id, plan.id, None)
    assert result.items[0].state == "conflict" and not result.closed
    assert reflection.status(sessions.current.id)["through_sequence"] == 0
    assert memory.read().blocks["human.md"] == "# Human\n"


async def test_source_and_plan_integrity_and_revision_conflicts(state):
    db, sessions, _, _ = state
    reflection, plan, _ = await proposal(state)
    stale = plan.model_copy(deep=True)
    changed = plan.model_copy(deep=True)
    changed.items[0].result_code = "reviewed_in_host"
    reflection.repository.save(changed, expected_revision=plan.revision)
    with pytest.raises(HistoryError, match="changed"):
        reflection.repository.save(stale, expected_revision=stale.revision)
    db._connection.execute(
        "UPDATE reflection_plans SET payload=replace(payload,'回复偏好','篡改偏好') WHERE id=?",
        (plan.id,),
    )
    db._connection.commit()
    with pytest.raises(HistoryError, match="invalid"):
        reflection.review(sessions.current.id)


async def test_source_changed_during_provider_call_does_not_publish_plan(state):
    db, sessions, memory, run = state
    provider = scripted([candidate(sessions.current.id, run)], [decision()])
    original = provider.generate

    async def tamper(*args, **kwargs):
        response = await original(*args, **kwargs)
        if len(provider.requests) == 3:
            db._connection.execute(
                "UPDATE messages SET payload=replace(payload,'已了解。','不同文本') WHERE run_id=?",
                (run,),
            )
            db._connection.commit()
        return response

    provider.generate = tamper
    reflection = service(db, memory, provider)
    with pytest.raises(HistoryError):
        await reflection.reflect(sessions.current.id)
    assert reflection.review(sessions.current.id) is None


async def test_budget_failure_does_not_issue_unbounded_provider_requests(state):
    db, sessions, memory, _ = state
    reflection = service(db, memory, FakeProvider(), input_tokens=2048)
    with pytest.raises(ReflectionError, match="source_too_large"):
        await reflection.reflect(sessions.current.id)
    assert reflection.provider.requests == []
    # Reflection-specific output budget must not break ordinary CLI construction.
    small = ReflectionService(
        reflection.repository,
        memory,
        FakeProvider(),
        ContextBudget(
            context_window=4096,
            reserved_output_tokens=512,
            safety_margin=128,
            summary_tokens=256,
            summary_output_tokens=512,
            compaction_input_tokens=2048,
        ),
    )
    assert small.status(sessions.current.id)["through_sequence"] == 0
    with pytest.raises(ReflectionError, match="reflection_budget"):
        await small.reflect(sessions.current.id)


async def test_secret_redaction_input_and_invalid_consolidation_are_nonwriting(state):
    db, sessions, memory, run = state
    seed(db, sessions.current.id, "OPENAI_API_KEY=sk-private-1234567890 password: hidden-value")
    provider = scripted([candidate(sessions.current.id, run)], [decision(candidate_id=2)])
    reflection = service(db, memory, provider)
    with pytest.raises(ReflectionError, match="invalid_candidate_coverage"):
        await reflection.reflect(sessions.current.id)
    assert "sk-private" not in provider.requests[0][1].text
    assert "hidden-value" not in provider.requests[0][1].text
    assert reflection.review(sessions.current.id) is None
    assert memory.read().blocks["human.md"] == "# Human\n"


def test_schema_three_to_four_migration_is_atomic_and_preserves_v06(tmp_path, monkeypatch):
    import sqlite3

    from personal_agent.storage import sqlite as storage

    path = tmp_path / "v06.sqlite3"
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 3)
        with SQLiteStore(path) as db:
            db.initialize()
            sessions = SessionManager(db)
            run = seed(db, sessions.current.id)
            original = db.load_messages(sessions.current.id)
    with monkeypatch.context() as patch:
        patch.setitem(storage.MIGRATIONS, 4, (*storage.MIGRATIONS[4], "INVALID SQL"))
        with SQLiteStore(path) as db:
            with pytest.raises(HistoryError):
                db.initialize()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='reflection_plans'"
        ).fetchone()
    with SQLiteStore(path) as db:
        db.initialize()
        assert db.load_messages(sessions.current.id) == original
        assert db.context_page(sessions.current.id, 0).turns[0].run_id == run
        assert ReflectionRepository(db, "heybuddy").progress(sessions.current.id) == 0
        assert len(db.search_recall("heybuddy", "长期", 5)) == 1


async def test_cli_plan_restart_apply_and_crash_after_file_publication(tmp_path):
    import os
    import subprocess
    import sys

    root = PROJECT_ROOT
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("OPENAI_", "AGENT_"))
    }
    environment.update(PYTHONPATH=os.pathsep.join([str(root), str(root / "src")]), PYTHONUTF8="1")
    bootstrap = r"""
import asyncio, os, sys
from pathlib import Path
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.sessions import SessionManager
from personal_agent.memory.store import MemoryFS
from personal_agent.core.agent import Agent
from personal_agent.core.budget import ContextBudget
from personal_agent.tools.registry import ToolRegistry
from personal_agent.main import chat
from tests.fakes import FakeProvider
from tests.test_reflection import seed, scripted, candidate, decision, service

with SQLiteStore(Path('restart.sqlite3')) as db:
    db.initialize()
    sessions=SessionManager(db,budget=ContextBudget(context_window=64000))
    memory=MemoryFS.for_agent(Path('memory'),'heybuddy')
    memory.initialize()
    sessions.memory=memory
    mode=sys.argv[1]
    if mode=='plan':
        run=seed(db,sessions.current.id)
        reflection=service(db,memory,scripted([candidate(sessions.current.id,run)],[decision()]))
        inputs=iter(['/reflect','quit'])
    else:
        reflection=service(db,memory,FakeProvider())
        plan=reflection.review(sessions.current.id)
        assert plan is not None
        if mode=='crash':
            original=memory.edit
            def exit_after_save(*args,**kwargs):
                original(*args,**kwargs)
                os._exit(73)
            memory.edit=exit_after_save
            inputs=iter(['/reflect apply '+plan.id+' all'])
        else:
            assert plan.items[0].state=='applying'
            original=memory.edit
            def never(*args,**kwargs):
                raise AssertionError('Never repeat previously published memory')
            memory.edit=never
            inputs=iter(['/reflect apply '+plan.id+' all',
                         '/reflect reconcile '+plan.id+' 1','/reflect status','quit'])
    agent=Agent(FakeProvider(),ToolRegistry(),context=sessions.context())
    asyncio.run(chat(agent,read=lambda _:next(inputs),sessions=sessions,reflection=reflection))
    if mode=='plan':
        assert memory.read().blocks['human.md']=='# Human\n'
        assert reflection.status(sessions.current.id)['through_sequence']==0
    else:
        assert '回答先给结论' in memory.read().blocks['human.md']
        assert reflection.status(sessions.current.id)['through_sequence']==2
        assert reflection.review(sessions.current.id) is None
"""
    for mode, expected in [("plan", 0), ("crash", 73), ("recover", 0)]:
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", bootstrap, mode],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        assert result.returncode == expected, (mode, result.stderr, result.stdout)
    with SQLiteStore(tmp_path / "restart.sqlite3") as db:
        db.initialize()
        sessions = SessionManager(db)
        assert len(db.load_messages(sessions.current.id)) == 2
        assert (
            len(db.list_runs(sessions.current.id)) == 1
        )  # Reflection commands never add chat Runs.


async def test_metadata_edit_partial_cannot_be_reconciled_while_parent_is_stale(state):
    _, sessions, memory, _ = state
    index.create(memory, "projects/heybuddy.md", TOPIC)
    parent = memory.path("projects/MEMORY.md")
    content = parent.read_text(encoding="utf-8")
    # Preserve a user's unmarked index entry; changing topic metadata requires review.
    import re

    parent.write_text(
        re.sub(r" <!-- heybuddy-index:[0-9a-f]{64} -->", "", content), encoding="utf-8"
    )
    change = decision(
        path="projects/heybuddy.md",
        old_text="HeyBuddy 架构决策",
        new_text="HeyBuddy 当前架构和开放问题",
    )
    reflection, plan, _ = await proposal(state, changes=[change], paths=["projects/heybuddy.md"])
    result = reflection.apply(sessions.current.id, plan.id, None)
    assert result.items[0].state == "partial"
    assert result.items[0].index_issue["reason"] == "custom_or_unmanaged_link"
    assert not reflection.reconcile(sessions.current.id, plan.id, 1).closed
    raw = parent.read_text(encoding="utf-8")
    parent.write_text(
        raw.replace("HeyBuddy 架构决策", "HeyBuddy 当前架构和开放问题"), encoding="utf-8"
    )
    assert reflection.reconcile(sessions.current.id, plan.id, 1).closed
