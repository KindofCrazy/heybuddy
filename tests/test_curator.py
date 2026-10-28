import json

import pytest

from personal_agent.core.models import ModelResponse
from personal_agent.learning.curator import CuratorPolicy, MemoryCurator
from personal_agent.memory import index
from tests.fakes import FakeProvider
from tests.test_learning import learner, script, state
from tests.test_organization_schedule import FakeClock
from tests.test_reflection import TOPIC, candidate, seed

__all__ = ["state"]


def curator(state, changes, **policy):
    learning = learner(state, FakeProvider(ModelResponse(text=json.dumps({"changes": changes}))))
    learning.clock = FakeClock()
    return MemoryCurator(learning, CuratorPolicy(**policy))


def change(
    path="projects/heybuddy.md",
    old="- 采用 MemoryFS。\n- 采用 MemoryFS。",
    new="- 采用 MemoryFS。",
    kind="deduplicate",
    **extra,
):
    return dict(
        candidate_id=1,
        action="update",
        kind=kind,
        path=path,
        old_text=old,
        new_text=new,
        reason="只保留一份当前知识",
        **extra,
    )


async def test_deduplicate_and_repeated_run_is_no_meaningless_write(state):
    db, sessions, memory, run = state
    index.create(memory, "projects/heybuddy.md", TOPIC + "\n- 采用 MemoryFS。\n- 采用 MemoryFS。\n")
    service = curator(state, [change()])
    result = await service.run(force=True)
    assert result["outcome"] == "completed" and result["updated_files"] == 1
    assert memory.read_file("projects/heybuddy.md")["content"].count("- 采用 MemoryFS。") == 1
    assert (await service.run(force=True))["unchanged"]
    assert len(service.model.provider.requests) == 1


async def test_reconcile_current_state_requires_actual_related_user_evidence(state):
    db, sessions, memory, run = state
    index.create(memory, "projects/heybuddy.md", TOPIC + "\n- 正在准备算法实习。\n")
    service = curator(state, [])
    newer = seed(db, sessions.current.id, "明确更正：我已经获得算法实习 Offer。")
    service.learning.model.provider = script(
        [
            candidate(
                sessions.current.id,
                newer,
                sequence=3,
                quote="明确更正：我已经获得算法实习 Offer。",
                content="用户已补充实习进展",
            )
        ],
        [{"path": "projects/heybuddy.md", "candidate_ids": [1]}],
        [
            {
                "action": "update",
                "old_text": "采用方案 A。",
                "new_text": "采用方案 A。\n- 用户已补充实习进展。",
                "reason": "记录已提供的相关进展",
            }
        ],
    )
    await service.learning.learn(run_ids=[newer])
    edit = change(
        old="- 正在准备算法实习。",
        new="- 已经获得算法实习 Offer。",
        kind="reconcile",
        evidence=[
            dict(
                conversation_id=sessions.current.id,
                run_id=newer,
                sequence=3,
                quote="我已经获得算法实习 Offer。",
            )
        ],
    )
    service.model.provider = FakeProvider(ModelResponse(text=json.dumps({"changes": [edit]})))
    result = await service.run(force=True)
    assert result["outcome"] == "completed"
    assert service.repository.get(result["job_id"])["sources"]["runs"][0]["id"] == newer
    text = memory.read_file("projects/heybuddy.md")["content"]
    assert "正在准备" not in text and "已经获得" in text


@pytest.mark.parametrize("mode", ["stable_old_preference", "unresolved_conflict"])
async def test_old_preferences_and_unresolved_conflicts_are_preserved(state, mode):
    db, sessions, memory, run = state
    text = (
        TOPIC
        + "\n- 用户喜欢简洁。\n"
        + ("- 长回答与简短回答偏好尚待确认。" if mode.endswith("conflict") else "")
    )
    index.create(memory, "projects/heybuddy.md", text)
    service = curator(state, [])
    service.clock.advance(86400 * 90)
    assert (await service.run(force=True))["updated_files"] == 0
    assert memory.read_file("projects/heybuddy.md")["content"] == text


def split_changes():
    section = "## Recall\n- 原始 Run 是历史证据。\n- Markdown 保存当前知识。\n"
    create = dict(
        candidate_id=1,
        action="create",
        kind="split",
        path="topics/recall.md",
        new_text="---\nname: recall\ndescription: 历史检索\n---\n\n" + section,
        reason="拆出独立章节",
        source_path="projects/heybuddy.md",
        source_text=section,
    )
    update = change(old=section, new="[Recall](topics/recall.md)\n", kind="split")
    return section, [update, create]


async def test_split_saves_and_indexes_destination_before_old_content_removal(state, monkeypatch):
    db, sessions, memory, run = state
    section, changes = split_changes()
    index.create(memory, "projects/heybuddy.md", TOPIC + section)
    service = curator(state, changes)
    original = index.edit

    def check(fs, path, *args, **kwargs):
        assert memory.path("topics/recall.md").exists()
        assert index.inspect(memory)["ok"]
        return original(fs, path, *args, **kwargs)

    monkeypatch.setattr(index, "edit", check)
    assert (await service.run(force=True))["updated_files"] == 2
    assert section in memory.read_file("topics/recall.md")["content"]
    assert section not in memory.read_file("projects/heybuddy.md")["content"]


async def test_long_split_keeps_source_when_destination_requires_review(state):
    db, sessions, memory, _ = state
    section, changes = split_changes()
    original = TOPIC + section + ("保留其他内容。\n" * 100)
    index.create(memory, "projects/heybuddy.md", original)
    index.create(
        memory,
        "topics/old-recall.md",
        "---\nname: recall\ndescription: 已有同名主题\n---\n\n已有内容。\n",
    )
    service = curator(state, changes, long_file_bytes=1024)
    result = await service.run(force=True)
    assert result["outcome"] == "failed" and result["error_code"] == "split_target_requires_review"
    assert memory.read_file("projects/heybuddy.md")["content"] == original
    assert not memory.path("topics/recall.md").exists()


async def test_split_partial_index_failure_keeps_old_knowledge_and_explicit_safe_retry(
    state, monkeypatch
):
    db, sessions, memory, run = state
    section, changes = split_changes()
    index.create(memory, "projects/heybuddy.md", TOPIC + section)
    service = curator(state, changes)
    original = index.ensure_chain

    def fail(*args):
        raise OSError("injected index failure")

    monkeypatch.setattr(index, "ensure_chain", fail)
    result = await service.run(force=True)
    assert result["outcome"] == "requires_reconciliation"
    assert section in memory.read_file("projects/heybuddy.md")["content"]
    assert section in memory.read_file("topics/recall.md")["content"]
    assert service.learning.retry_job(result["job_id"])["error_code"] == "index_incomplete"
    monkeypatch.setattr(index, "ensure_chain", original)
    index.repair(memory)
    assert service.learning.retry_job(result["job_id"])["outcome"] == "pending"
    # A fresh plan sees the existing destination; it only updates the old file.
    edit = change(old=section, new="[Recall](topics/recall.md)\n", kind="organization")
    service.model.provider = FakeProvider(ModelResponse(text=json.dumps({"changes": [edit]})))
    resumed = await service.run(force=True, retry=True)
    assert resumed["outcome"] == "completed", resumed
    assert section in memory.read_file("topics/recall.md")["content"]
    assert index.inspect(memory)["ok"]


@pytest.mark.parametrize("mode", ["persona", "delete", "content_loss", "unproven_reconciliation"])
async def test_curator_rejects_dangerous_or_unproved_edits(state, mode):
    db, sessions, memory, run = state
    index.create(memory, "projects/heybuddy.md", TOPIC)
    edits = [change(old="采用方案 A。", new="删除全部事实。", kind="organization")]
    if mode == "persona":
        edits = [change(path="persona.md", old="# Persona", new="# Other", kind="organization")]
    elif mode == "delete":
        edits[0]["action"] = "delete"
    elif mode == "unproven_reconciliation":
        edits[0]["kind"] = "reconcile"
    service = curator(state, edits)
    persona = memory.read().blocks["persona.md"]
    assert (await service.run(force=True))["outcome"] == "failed"
    assert memory.read_file("projects/heybuddy.md")["content"] == TOPIC
    assert memory.read().blocks["persona.md"] == persona


async def test_low_frequency_trigger_requires_updates_or_long_file_not_daily_llm(state):
    service = curator(state, [], update_threshold=2)
    service.clock.advance(86401)
    assert not service.due()
    assert not service.model.provider.requests
    with service.repository.store._transaction() as db:
        db.execute("UPDATE memory_learning_state SET updates=2 WHERE agent_id='heybuddy'")
    assert not service.due()
    service.clock.advance(86401)
    assert service.due()
    assert (await service.run())["outcome"] == "completed"
    assert not service.due()
