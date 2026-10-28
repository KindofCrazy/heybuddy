"""Optional selection cannot erase deterministic evidence, even under failure/budget pressure."""

import asyncio
import json

import pytest

from personal_agent.core.models import ModelResponse
from personal_agent.core.retrieval import RetrievalItem
from personal_agent.llm.base import ProviderTimeout
from personal_agent.memory import index
from personal_agent.memory.retrieval import MemoryRetrievalPolicy
from tests.fakes import FakeProvider
from tests.test_learning import state
from tests.test_memory_retrieval import references, runtime, selection
from tests.test_threads import seed_days, thread_service

__all__ = ["state"]


def topics(memory, *, large=False):
    for path, description, content in (
        ("topics/alpha.md", "研究方向", "ALPHA_EVIDENCE"),
        ("topics/other.md", "研究方向", "OTHER_EVIDENCE"),
        ("topics/beta.md", "邻接结构", "BETA_EVIDENCE" + (" stable state" * 500 if large else "")),
    ):
        name = path.split("/")[-1].removesuffix(".md")
        index.create(
            memory, path, f"---\nname: {name}\ndescription: {description}\n---\n\n{content}\n"
        )


def retriever(state, response):
    selector = FakeProvider(response)
    agent, _ = runtime(
        state,
        FakeProvider(ModelResponse(text="main")),
        selection(selector),
        policy=MemoryRetrievalPolicy(file_limit=3, section_bytes=16384),
    )
    return agent.context.retriever, selector


@pytest.mark.parametrize(
    "response",
    [
        ModelResponse(text='{"ids":[]}'),
        ModelResponse(text='{"ids":["memory:unknown"]}'),
        ModelResponse(text='{"ids":["thread:other-agent"]}'),
        ModelResponse(text='{"ids":["memory:topics/alpha.md","memory:topics/alpha.md"]}'),
        ModelResponse(text=json.dumps({"ids": ["memory:topics/alpha.md"] * 7})),
        ModelResponse(text="invalid"),
        ProviderTimeout("synthetic timeout"),
    ],
)
async def test_valid_lexical_baseline_survives_empty_invalid_or_failed_preflight(state, response):
    topics(state[2])
    lookup, provider = retriever(state, response)
    query = "我的研究方向是什么？"
    baseline = lookup.retrieve(query, frozenset())
    assert baseline.candidates and len(baseline.items) == 2
    before = baseline.model_copy(deep=True)
    result = await lookup.preflight(query, frozenset(), baseline)
    assert result.items == before.items
    assert baseline == before  # Failure/mutation never touches the caller's baseline.
    assert len(provider.requests) == 1
    assert result.metrics["extra_requests"] == 1


async def test_additions_are_deduplicated_and_cannot_displace_baseline(state):
    topics(state[2])
    lookup, _ = retriever(state, ModelResponse(text='{"ids":["memory:topics/beta.md"]}'))
    baseline = lookup.retrieve("我的研究方向是什么？", frozenset())
    result = await lookup.preflight("我的研究方向是什么？", frozenset(), baseline)
    paths = [r.reference["path"] for r in result.items]
    assert paths == ["topics/alpha.md", "topics/other.md", "topics/beta.md"]
    assert len(result.items) <= 8


async def test_thread_episode_and_raw_evidence_survive_provider_failure(state):
    db, sessions, memory, _ = state
    repo = thread_service(db)
    await seed_days(db, sessions.new().id, repo.repository.episodes, days=3)
    await repo.organize()
    topics(memory)
    lookup, selector = retriever(state, ProviderTimeout("synthetic timeout"))
    baseline = lookup.retrieve("继续之前 HeyBuddy 的研究方向。", frozenset())
    assert any(i.reference["kind"] == "thread" for i in baseline.items)
    assert any(i.reference["kind"] == "episode" for i in baseline.items)
    assert any(i.turn for i in baseline.items)
    # Host candidate catalogue is already Agent-scoped; force a selection attempt
    # even when the historical matcher could answer without one.
    baseline.candidates = [
        {"id": "memory:topics/alpha.md", "kind": "memory", "path": "topics/alpha.md"}
    ]
    result = await lookup.preflight("继续之前 HeyBuddy 的研究方向。", frozenset(), baseline)
    assert result.items == baseline.items and len(selector.requests) == 1


async def test_empty_baseline_and_empty_choice_remain_recall_fallback(state):
    topics(state[2])
    lookup, _ = retriever(state, ModelResponse(text='{"ids":[]}'))
    baseline = lookup.retrieve("我们上次对那件事情最后怎么决定的？", frozenset())
    assert not baseline.items
    result = await lookup.preflight("我们上次对那件事情最后怎么决定的？", frozenset(), baseline)
    assert not result.items and result.path == "recall_fallback"


async def test_unfitted_supplement_does_not_remove_thread_state_or_current_input(
    state, monkeypatch
):
    topics(state[2], large=True)
    selector = FakeProvider(ModelResponse(text='{"ids":["memory:topics/beta.md"]}'))
    main = FakeProvider(ModelResponse(text="结果"))
    agent, _ = runtime(
        state,
        main,
        selection(selector),
        policy=MemoryRetrievalPolicy(file_limit=3, section_bytes=16384),
    )
    agent.context.optional_history_tokens = 2048
    original = agent.context.retriever.retrieve

    def baseline(*args):
        result = original(*args)
        result.items.insert(
            0,
            RetrievalItem(
                reference={"kind": "thread", "id": "fixed-thread", "current_state": "stable state"}
            ),
        )
        return result

    monkeypatch.setattr(agent.context.retriever, "retrieve", baseline)
    question = "我的研究方向是什么？"
    assert (await agent.run(question)).status == "completed"
    refs = references(main.requests[0])
    assert next(r for r in refs if r["kind"] == "thread")["current_state"] == "stable state"
    assert not any(r.get("path") == "topics/beta.md" for r in refs)
    assert main.requests[0][-1].text == question
    assert agent.context.diagnostics()["retrieval"]["budget_omitted"]


async def test_custom_preflight_mutation_failure_keeps_baseline_and_cancellation_propagates(state):
    topics(state[2])
    main = FakeProvider(ModelResponse(text="结论"))
    agent, _ = runtime(state, main)

    async def fail(query, excluded, result):
        result.items.clear()
        raise ValueError("custom preflight")

    agent.context.retriever.preflight = fail
    assert (await agent.run("我的研究方向是什么？")).status == "completed"
    assert references(main.requests[0])

    async def cancelled(*args):
        raise asyncio.CancelledError

    agent.context.retriever.preflight = cancelled
    with pytest.raises(asyncio.CancelledError):
        await agent.run("我的研究方向是什么？")
    assert len(main.requests) == 1


async def test_history_that_looks_like_host_json_is_never_parsed_or_mutated():
    from personal_agent.core.agent import Agent
    from personal_agent.core.budget import ContextBudget
    from personal_agent.core.context import ContextBuilder
    from personal_agent.core.history import HistoryTurn
    from personal_agent.core.models import AssistantMessage, TextMessage
    from personal_agent.core.retrieval import RetrievalResult
    from personal_agent.tools.builtin import create_builtin_registry

    payload = '{"kind": "working_context_reference", "malformed": '
    historical = HistoryTurn(
        run_id="historical-json",
        first_sequence=1,
        last_sequence=2,
        messages=[TextMessage(role="user", text=payload), AssistantMessage(text="历史回答")],
        source_hash="synthetic",
    )

    class Retrieval:
        def retrieve(self, *args):
            return RetrievalResult(
                items=[RetrievalItem(reference={"kind": "historical_run"}, turn=historical)]
            )

    provider = FakeProvider(ModelResponse(text="继续核验"))
    context = ContextBuilder(retriever=Retrieval(), budget=ContextBudget(context_window=64000))
    assert (
        await Agent(provider, create_builtin_registry(), context=context).run("继续讨论")
    ).status == "completed"
    assert any(getattr(m, "text", None) == payload for m in provider.requests[0])
    assert historical.messages[0].text == payload
    assert context.diagnostics()["retrieved_items"] == 1
