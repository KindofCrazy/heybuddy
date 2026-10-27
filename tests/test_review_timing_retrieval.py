import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from personal_agent.core.agent import Agent
from personal_agent.core.budget import ContextBudget, estimate_request
from personal_agent.core.models import (
    AssistantMessage,
    ModelResponse,
    RunResult,
    TextMessage,
    ToolMessage,
    ToolResult,
)
from personal_agent.retrieval import ContextRetriever
from personal_agent.sessions import SessionManager
from personal_agent.storage.threads import ThreadRepository
from personal_agent.tools.builtin import create_builtin_registry
from tests.fakes import FakeProvider, add_call
from tests.test_episodes import SegmentProvider, organizer, seed, state
from tests.test_threads import seed_days, thread_service
from tests.test_working_context import references

__all__ = ["state"]


def long_tool(db, stream, started, ended):
    run_id = db.begin_run(stream, TextMessage(role="user", text="HeyBuddy 执行较长的计算"))
    call = add_call("long-tool")
    db.append_message(run_id, AssistantMessage(tool_calls=[call]))
    db.append_message(
        run_id,
        ToolMessage(result=ToolResult(call_id=call.id, name=call.name, ok=True, data={"sum": 5})),
    )
    db.append_message(run_id, AssistantMessage(text="结果为 5"))
    db.finish_run(run_id, RunResult(status="completed", rounds=2))
    with db._transaction() as connection:
        connection.execute(
            "UPDATE runs SET started_at=?,ended_at=? WHERE id=?",
            (started.isoformat(), ended.isoformat(), run_id),
        )
    return run_id


@pytest.mark.parametrize(
    "gap,duration,semantic_split,expected",
    [
        (14400, 12600, False, 2),  # End-to-start gap is only 30 min; start gap is 4 h.
        (7200, 6600, False, 1),  # Cross local midnight; semantic continuity may remain.
        (60, 10, True, 2),  # Short gap can still be a different topic.
        (345600, 180, False, 2),  # Same topic days later, distinct Episodes.
    ],
)
async def test_input_gap_not_tool_duration_controls_candidate_boundaries(
    state,
    gap,
    duration,
    semantic_split,
    expected,
):
    db, stream, repo = state
    zone = timezone(timedelta(hours=8))
    start = datetime(2025, 1, 1, 23, 0, tzinfo=zone)
    first = long_tool(db, stream, start, start + timedelta(seconds=duration))
    second = seed(db, stream, text="继续 HeyBuddy 同一项目", at=start + timedelta(seconds=gap))
    original = db.load_messages(stream)
    provider = SegmentProvider(split=semantic_split)
    assert (await organizer(repo, provider).organize(stream))["outcome"] == "completed"
    assert len(repo.list(stream)) == expected
    boundary = json.loads(provider.requests[0][1].text)["boundaries"][0]
    assert boundary["start_gap_seconds"] == gap
    assert boundary["previous_duration_seconds"] == duration
    assert (boundary["strength"] == "required") == (gap > 10800)
    assert db.load_messages(stream) == original
    if gap == 345600:
        service = thread_service(db)
        await service.organize()
        assert len({e.thread_id for e in repo.list(stream)}) == 1
        assert {r for e in repo.list(stream) for r in e.run_ids} == {first, second}


async def test_provisional_tail_does_not_append_across_long_input_gap_hidden_by_tool_duration(
    state,
):
    db, stream, repo = state
    start = datetime(2025, 1, 1, 10, tzinfo=UTC)
    ended = start + timedelta(hours=3, minutes=30)
    first = long_tool(db, stream, start, ended)
    service = organizer(repo)
    await service.organize(stream, at=ended)
    old = repo.list(stream)[0]
    assert old.status == "provisional"
    second = seed(db, stream, at=start + timedelta(hours=4))
    await service.organize(stream, at=start + timedelta(hours=4, minutes=1))
    assert len(repo.list(stream)) == 2
    assert repo.get(old.id).run_ids == [first] and repo.get(old.id).status == "finalized"
    assert any(e.run_ids == [second] for e in repo.list(stream))


async def unlinked_update(state):
    db, stream, repo = state
    await seed_days(db, stream, repo)
    threads = thread_service(db)
    await threads.organize()
    old_thread = threads.repository.list()[0]
    new_run = seed(
        db, stream, text="HeyBuddy 自动整理记忆机制，最后决定 Idle 先整理 Episode，再更新 Thread。"
    )

    def describe(output, data):
        output["episodes"][0]["title"] = "HeyBuddy Memory 设计"
        output["episodes"][0]["summary"] = (
            "自动整理记忆机制最后决定：Idle 先整理 Episode，再更新 Thread。"
        )

    await organizer(repo, SegmentProvider(mutate=describe)).organize(stream)
    new_episode = next(e for e in repo.list(stream) if new_run in e.run_ids)
    return old_thread, new_episode, new_run


async def test_thread_hit_supplements_new_unassigned_memory_episode_without_writing_links(state):
    db, _, repo = state
    old, latest, run_id = await unlinked_update(state)
    retrieval = ContextRetriever(ThreadRepository(db, "heybuddy"))
    result = retrieval.retrieve("继续之前 HeyBuddy 的 Memory 设计。", frozenset())
    assert result.path == "thread"
    episodes = [item.reference for item in result.items if item.reference["kind"] == "episode"]
    assert episodes[0]["id"] == latest.id
    assert episodes[0]["thread_id"] is None and episodes[0]["retrieved_via"] == "lexical_supplement"
    assert result.items[0].reference["newer_unlinked_evidence"]
    assert any(item.turn and item.turn.run_id == run_id for item in result.items)
    assert repo.get(latest.id).thread_id is None
    assert retrieval.repository.get(old.id) == old


async def test_implicit_history_question_can_match_episode_summary_or_explicitly_fall_back(state):
    db, _, _ = state
    _, latest, _ = await unlinked_update(state)
    retrieval = ContextRetriever(ThreadRepository(db, "heybuddy"))
    result = retrieval.retrieve(
        "我们之前讨论的那个自动整理记忆的机制，最后决定怎么做？", frozenset()
    )
    assert result.path == "episode"
    assert any(item.reference.get("id") == latest.id for item in result.items)
    missing = retrieval.retrieve("之前 qzxvUnknownSubject 还有什么证据", frozenset())
    assert missing.path == "recall_fallback" and not missing.items


async def test_supplement_respects_context_budget_recent_run_dedup_and_single_input(state):
    db, _, _ = state
    _, _, run_id = await unlinked_update(state)
    sessions = SessionManager(db, budget=ContextBudget(context_window=64000))
    sessions.retriever = ContextRetriever(ThreadRepository(db, "heybuddy"))
    provider = FakeProvider(ModelResponse(text="先整理 Episode，再更新 Thread。"))
    agent = Agent(provider, create_builtin_registry(), context=sessions.context())
    query = "继续之前 HeyBuddy 的 Memory 设计。"
    assert (await agent.run(query)).status == "completed"
    request = provider.requests[0]
    assert len(provider.requests) == 1
    assert estimate_request(request, provider.tools[0]) <= sessions.budget.input_limit
    assert sum(getattr(m, "text", "") == query for m in request) == 1
    assert not any(r["source"].get("run_id") == run_id for r in references(request))
    assert any(
        r["source"].get("retrieved_via") == "lexical_supplement" for r in references(request)
    )
