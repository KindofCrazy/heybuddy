"""Receipt windows recover from current files without replaying old patches."""

import asyncio

import pytest

from personal_agent.core.models import TextMessage
from personal_agent.memory import index
from personal_agent.organization.runtime import OrganizationRuntime
from personal_agent.reflection.memory import catalogue, plan_item
from personal_agent.reflection.models import Decision
from personal_agent.reflection.writer import write_item
from tests.fakes import FakeProvider
from tests.test_episodes import organizer
from tests.test_learning import human_write, learner, script, state
from tests.test_organization_schedule import FakeClock, pump
from tests.test_reflection import candidate, seed

__all__ = ["state"]
TARGET = "knowledge/preferences.md"
BODY = "---\nname: preferences\ndescription: 已确认的回复偏好\n---\n\n- 尊重用户的简洁偏好。\n"


def plans(memory):
    core = memory.read().blocks
    if memory.path(TARGET).exists():
        core[TARGET] = memory.read_file(TARGET)["content"]
        target = Decision(
            candidate_id=2,
            action="update",
            path=TARGET,
            old_text="尚未整理。",
            new_text="尊重用户的简洁偏好。",
            reason="明确偏好",
        )
    else:
        target = Decision(
            candidate_id=2, action="create", path=TARGET, new_text=BODY, reason="明确偏好"
        )
    return [
        plan_item(
            memory,
            1,
            None,
            Decision(candidate_id=1, path="human.md", **human_write()),
            core,
            catalogue(memory),
        ),
        plan_item(
            memory,
            2,
            None,
            target,
            core,
            catalogue(memory),
        ),
    ]


async def interrupt(state, monkeypatch, *, before=False):
    import personal_agent.learning.service as module

    db, sessions, memory, run = state
    learning = learner(state, FakeProvider())
    refs, records, hints = learning.repository.snapshot([run], [], learning.policy)
    jid = learning.repository.create("priority", refs, learning.clock.now())
    items = plans(memory)

    async def propose():
        return items

    original_save = learning.repository.save

    def cancel_before(*args, **kwargs):
        original_save(*args, **kwargs)
        if len(kwargs.get("operations", [])) == 2:
            asyncio.current_task().cancel()

    original_write = module.write_item

    def cancel_after(*args):
        original_write(*args)
        asyncio.current_task().cancel()

    with monkeypatch.context() as patch:
        if before:
            patch.setattr(learning.repository, "save", cancel_before)
        else:
            patch.setattr(module, "write_item", cancel_after)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.create_task(
                learning._execute(jid, refs, records, hints, proposer=propose)
            )
    assert learning.repository.get(jid)["state"] == "requires_reconciliation"
    return learning, jid


@pytest.mark.parametrize("before", [True, False])
@pytest.mark.parametrize("existing", [True, False])
async def test_startup_replans_unpublished_files_and_does_not_repeat_published_update(
    state, monkeypatch, before, existing
):
    db, sessions, memory, run = state
    if existing:
        index.create(memory, TARGET, BODY.replace("尊重用户的简洁偏好。", "尚未整理。"))
    old, jid = await interrupt(state, monkeypatch, before=before)
    journal = db.load_messages(sessions.current.id)
    provider = script(
        [
            candidate(sessions.current.id, run),
            candidate(sessions.current.id, run, subject="主题偏好"),
        ],
        [{"path": "human.md", "candidate_ids": [1]}, {"path": TARGET, "candidate_ids": [2]}],
        [
            human_write() if before else {"action": "no_op", "reason": "已发布"},
            {
                "action": "update",
                "old_text": "尚未整理。",
                "new_text": "尊重用户的简洁偏好。",
                "reason": "尚未发布",
            }
            if existing
            else {"action": "create", "new_text": BODY, "reason": "尚未发布"},
        ],
    )
    recovered = learner(state, provider)
    assert not provider.requests  # Startup classification itself never calls a model.
    assert recovered.repository.get(jid)["state"] == "pending"
    assert memory.path(TARGET).exists() is existing
    assert recovered.repository.activation()["updates"] == 0
    assert (await recovered.learn_priority())["outcome"] == "completed"
    final = recovered.repository.get(jid)
    assert {op["path"] for op in final["operations"]} == {"human.md", TARGET}
    assert all(op["state"] == "applied" for op in final["operations"])
    assert recovered.repository.activation()["updates"] == 2
    assert memory.read().blocks["human.md"].count("回答先给结论") == 1
    assert memory.read_file(TARGET)["content"] == BODY
    assert "回答先给结论" in provider.requests[2][1].text if not before else True
    assert db.load_messages(sessions.current.id) == journal
    assert recovered.reflection.repository.progress(sessions.current.id) == 0
    assert (await recovered.learn(run_ids=[run]))["outcome"] == "completed"
    assert len(provider.requests) == 4
    new_run = seed(db, sessions.current.id, "新的一天，普通后续交流。")
    provider = script([])
    recovered.model.provider = provider
    assert (await recovered.learn(run_ids=[new_run]))["outcome"] == "completed"


async def test_conflict_is_bounded_persistent_visible_and_blocks_even_direct_writes(
    state, monkeypatch
):
    db, sessions, memory, run = state
    old, jid = await interrupt(state, monkeypatch)
    memory.edit(
        "human.md",
        memory.read().blocks["human.md"],
        "# Human\n人工决定保留。\n",
        expected_hash=memory.read().hashes["human.md"],
    )
    clock = FakeClock()
    old.clock = clock
    provider = FakeProvider()
    recovered = learner(state, provider)
    recovered.clock = clock
    for _ in range(4):
        clock.advance(86400)
        recovered.recover_pending()
    job = recovered.repository.get(jid)
    assert job["state"] == "requires_reconciliation" and job["error"] == "content_changed"
    assert job["metrics"]["recovery"]["attempts"] == 3
    assert not recovered.recovery_ready()
    assert not recovered.ready(idle=True)
    unrelated = seed(db, sessions.current.id, "无关主题，不能绕过未核实的跨文件依赖。")
    assert (await recovered.learn(run_ids=[unrelated]))["outcome"] == "blocked"
    assert not provider.requests and not memory.path(TARGET).exists()
    assert memory.read().blocks["human.md"] == "# Human\n人工决定保留。\n"
    assert any(
        r["id"] == jid and r["recovery_attempts"] == 3 for r in recovered.status()["attention"]
    )
    # A further restart does not reset attempts or spin on the same conflict.
    again = learner(state, FakeProvider())
    assert again.repository.get(jid)["metrics"]["recovery"]["attempts"] == 3


async def test_running_agent_defers_startup_and_idle_checks_until_foreground_finishes(
    state, monkeypatch
):
    db, sessions, memory, run = state
    _, jid = await interrupt(state, monkeypatch, before=True)
    active = db.begin_run(sessions.current.id, TextMessage(role="user", text="正在前台执行"))
    recovered = learner(state, FakeProvider())
    assert recovered.repository.get(jid)["state"] == "requires_reconciliation"
    assert not recovered.recovery_ready()
    db.interrupt_run(active, cancelled=True)
    provider = script([])
    recovered.model.provider = provider
    clock = FakeClock()
    recovered.clock = clock
    runtime = OrganizationRuntime(
        organizer(recovered.repository.episodes), learning=recovered, clock=clock
    )
    try:
        runtime.schedule(sessions.current.id)
        await pump()
        assert recovered.repository.get(jid)["state"] == "requires_reconciliation"
        clock.advance(1501)
        await pump(lambda: recovered.repository.get(jid)["state"] == "completed")
        assert runtime.last_report["memory_recovery"]["requests"] == 0
        assert len(provider.requests) == 1
    finally:
        await runtime.close()


async def test_after_hash_without_index_stays_protected_until_explicit_repair(state, monkeypatch):
    db, sessions, memory, run = state
    learning = learner(state, FakeProvider())
    refs, _, _ = learning.repository.snapshot([run], [], learning.policy)
    jid = learning.repository.create("priority", refs, learning.clock.now())
    item = plans(memory)[1]
    op = learning._receipt(item)
    learning.repository.save(jid, "processing", learning.clock.now(), operations=[op])
    with monkeypatch.context() as patch:
        patch.setattr(index, "ensure_chain", lambda *args: (_ for _ in ()).throw(OSError("index")))
        write_item(memory, item)
    assert item.state == "partial"
    recovered = learner(state, FakeProvider())
    assert recovered.repository.get(jid)["error"] == "index_incomplete"
    assert memory.read_file(TARGET)["content"] == BODY
    index.repair(memory)
    assert recovered.reconcile(jid)["outcome"] == "completed"
    assert not recovered.model.provider.requests
