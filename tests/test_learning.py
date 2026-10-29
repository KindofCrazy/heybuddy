"""Offline semantic scripts exercise host contracts, never assert real model quality."""

import asyncio
import json

import pytest

from personal_agent.core.history import HistoryError
from personal_agent.core.models import ModelResponse
from personal_agent.learning.models import LearningPolicy
from personal_agent.learning.service import MemoryLearningService
from personal_agent.memory import index
from personal_agent.reflection.models import ReflectionError
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.learning import LearningRepository
from tests.fakes import FakeProvider
from tests.test_reflection import FACT, TOPIC, candidate, seed, service, state

__all__ = ["state"]


def learner(state, provider, **policy):
    db, sessions, memory, run = state
    return MemoryLearningService(
        LearningRepository(EpisodeRepository(db, "heybuddy")),
        service(db, memory, provider),
        LearningPolicy(**policy),
    )


def script(candidates, groups=(), decisions=()):
    return FakeProvider(
        *(
            ModelResponse(text=json.dumps(data, ensure_ascii=False))
            for data in [{"candidates": candidates}, {"groups": list(groups)}, *decisions]
        )
    )


def human_write(new="回答先给结论，保持简洁。"):
    return dict(
        action="update",
        old_text="# Human\n",
        new_text="# Human\n" + new + "\n",
        reason="明确用户偏好",
    )


async def test_publication_callback_cannot_outlive_serial_scope(state):
    learning = learner(state, FakeProvider())
    db = state[0]
    before = db._connection.total_changes
    async with learning.publication() as publish:
        pass
    with pytest.raises(ReflectionError, match="publication_scope_closed"):
        await publish("unused", {}, [], [])
    assert db._connection.total_changes == before


async def test_preference_auto_apply_source_idempotency_and_no_reflection_progress(state):
    db, sessions, memory, run = state
    provider = script(
        [candidate(sessions.current.id, run)],
        [{"path": "human.md", "candidate_ids": [1]}],
        [human_write()],
    )
    learning = learner(state, provider)
    assert (await learning.learn(run_ids=[run]))["updated_files"] == 1
    assert FACT not in memory.read().blocks["human.md"]
    assert "回答先给结论" in memory.read().blocks["human.md"]
    assert (await learning.learn(run_ids=[run]))["outcome"] == "completed"
    assert len(provider.requests) == 3
    assert learning.reflection.repository.progress(sessions.current.id) == 0
    with db._transaction() as connection:
        rows = connection.execute("SELECT * FROM memory_learning_jobs").fetchall()
        assert len(rows) == 1 and FACT not in str(tuple(rows[0]))
        assert "回答先给结论" not in str(tuple(rows[0]))


async def test_heading_only_core_is_an_existing_update_target(state):
    _, sessions, memory, run = state
    provider = script(
        [candidate(sessions.current.id, run)],
        [{"path": "human.md", "candidate_ids": [1]}],
        [human_write()],
    )
    assert memory.read().blocks["human.md"] == "# Human\n"
    result = await learner(state, provider).learn(run_ids=[run])
    assert result["updated_files"] == 1
    request = json.loads(provider.requests[2][1].text)
    assert request["target_exists"] is True
    assert request["memory"]["human.md"] == "# Human\n"
    assert set(request["response_schema"]["properties"]["action"]["enum"]) == {
        "update",
        "no_op",
        "reject",
        "review",
    }


async def test_create_for_existing_core_is_rejected_without_writes_or_retry(state):
    _, sessions, memory, run = state
    before = memory.read()
    provider = script(
        [candidate(sessions.current.id, run)],
        [{"path": "human.md", "candidate_ids": [1]}],
        [{"action": "create", "new_text": "# Human\n新增资料。\n", "reason": "错误地新建核心"}],
    )
    result = await learner(state, provider).learn(run_ids=[run])
    assert result["outcome"] == "failed"
    assert result["error_code"] == "invalid_reflection_output"
    assert memory.read() == before
    assert len(provider.requests) == 3


async def test_project_group_consolidation_once_with_mixed_conversation_evidence(state):
    db, sessions, memory, run = state
    index.create(memory, "projects/heybuddy.md", TOPIC)
    first_stream = sessions.current.id
    second_stream = sessions.new().id
    second = seed(db, second_stream, "我们正式决定采用 MemoryFS。")
    candidates = [
        candidate(first_stream, run),
        candidate(
            second_stream,
            second,
            quote="我们正式决定采用 MemoryFS。",
            category="decision",
            content="采用 MemoryFS",
        ),
    ]
    provider = script(
        candidates,
        [{"path": "projects/heybuddy.md", "candidate_ids": [1, 2]}],
        [
            {
                "action": "update",
                "old_text": "采用方案 A。",
                "new_text": "采用 MemoryFS。",
                "reason": "合并项目决定和相关要求",
            }
        ],
    )
    result = await learner(state, provider).learn(run_ids=[run, second])
    assert result["updated_files"] == 1 and len(provider.requests) == 3
    assert "采用 MemoryFS" in memory.read_file("projects/heybuddy.md")["content"]


@pytest.mark.parametrize("mode", ["empty", "uncertain", "duplicate", "hypothetical", "suggestion"])
async def test_no_op_and_uncertain_do_not_write(state, mode):
    db, sessions, memory, run = state
    before = memory.read()
    candidates = (
        []
        if mode in {"empty", "hypothetical", "suggestion"}
        else [
            candidate(
                sessions.current.id,
                run,
                confidence="uncertain" if mode == "uncertain" else "confirmed",
            )
        ]
    )
    provider = script(
        candidates,
        [{"path": "human.md", "candidate_ids": [1]}],
        [
            human_write()
            if mode == "uncertain"
            else {"action": "no_op", "reason": "已记录或没有确认信息"}
        ],
    )
    result = await learner(state, provider).learn(run_ids=[run])
    assert result["outcome"] == "completed" and result["updated_files"] == 0
    assert memory.read() == before
    assert len(provider.requests) == (1 if not candidates else 3)


@pytest.mark.parametrize("bad", ["persona", "index", "path", "evidence", "assistant"])
async def test_forbidden_targets_or_evidence_stop_before_write(state, bad):
    db, sessions, memory, run = state
    before = memory.read()
    c = candidate(sessions.current.id, run)
    if bad == "evidence":
        c["evidence"][0]["quote"] = "not actually supplied"
    if bad == "assistant":
        c["evidence"][0].update(sequence=2, quote="已了解。")
    path = {"persona": "persona.md", "index": "MEMORY.md", "path": "../../escape.md"}.get(
        bad, "human.md"
    )
    learning = learner(state, script([c], [{"path": path, "candidate_ids": [1]}], [human_write()]))
    result = await learning.learn(run_ids=[run])
    assert result["outcome"] == "failed" and memory.read() == before


async def test_cancel_restores_pending_and_failed_needs_explicit_retry(state):
    entered = asyncio.Event()

    class Waiting:
        async def generate(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    run = state[-1]
    learning = learner(state, Waiting())
    task = asyncio.create_task(learning.learn(run_ids=[run]))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    learning.model.provider = FakeProvider(ModelResponse(text="bad"))
    assert (await learning.learn(run_ids=[run]))["outcome"] == "failed"
    provider = FakeProvider()
    learning.model.provider = provider
    assert (await learning.learn(run_ids=[run]))["outcome"] == "failed"
    assert not provider.requests
    learning.model.provider = script([])
    assert (await learning.learn(run_ids=[run], retry=True))["outcome"] == "completed"


async def test_file_success_then_sqlite_failure_reconcile_never_rewrites(state, monkeypatch):
    db, sessions, memory, run = state
    learning = learner(
        state,
        script(
            [candidate(sessions.current.id, run)],
            [{"path": "human.md", "candidate_ids": [1]}],
            [human_write()],
        ),
    )
    save = learning.repository.save

    def fail(jid, state, now, **kwargs):
        if any(op["state"] == "applied" for op in kwargs.get("operations", [])):
            raise HistoryError("simulated commit failure")
        return save(jid, state, now, **kwargs)

    monkeypatch.setattr(learning.repository, "save", fail)
    with pytest.raises(HistoryError):
        await learning.learn(run_ids=[run])
    assert "回答先给结论" in memory.read().blocks["human.md"]
    monkeypatch.setattr(learning.repository, "save", save)
    learning.repository.recover(learning.clock.now())
    with db._transaction() as connection:
        jid = connection.execute("SELECT id FROM memory_learning_jobs").fetchone()[0]
    assert (await learning.learn(run_ids=[run]))["outcome"] == "requires_reconciliation"
    assert learning.reconcile(jid)["outcome"] == "completed"
    assert memory.read().blocks["human.md"].count("回答先给结论") == 1
