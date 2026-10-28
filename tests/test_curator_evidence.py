"""File-associated source receipts, not recency alone, determine curation evidence."""

import json
from datetime import timedelta

import pytest

from personal_agent.core.models import ModelResponse
from personal_agent.learning.curator import MemoryCurator
from personal_agent.memory import index
from tests.fakes import FakeProvider
from tests.test_curator import change
from tests.test_learning import learner, script, state
from tests.test_organization_schedule import FakeClock
from tests.test_reflection import candidate, seed

__all__ = ["state"]


async def publish(state, learning, path, text):
    db, sessions, memory, _ = state
    run = seed(db, sessions.current.id, text)
    with db._transaction() as sql:
        sequence = sql.execute(
            "SELECT MIN(sequence) FROM messages WHERE run_id=?", (run,)
        ).fetchone()[0]
        at = learning.clock.now()
        sql.execute(
            "UPDATE runs SET started_at=?,ended_at=? WHERE id=?",
            (at.isoformat(), (at + timedelta(seconds=1)).isoformat(), run),
        )
    if memory.path(path).exists():
        old = memory.read_file(path)["content"]
        decision = {
            "action": "update",
            "old_text": old,
            "new_text": old + "\n- " + text + "\n",
            "reason": "新来源进展",
        }
    else:
        name = path.split("/")[-1].removesuffix(".md")
        decision = {
            "action": "create",
            "new_text": f"---\nname: {name}\ndescription: {name} 项目\n---\n\n# 当前\n- {text}\n",
            "reason": "明确用户事实",
        }
    learning.model.provider = script(
        [candidate(sessions.current.id, run, sequence=sequence, quote=text[:512], content=text)],
        [{"path": path, "candidate_ids": [1]}],
        [decision],
    )
    assert (await learning.learn(run_ids=[run]))["updated_files"] == 1
    learning.clock.advance(86400)
    return run


async def test_old_project_evidence_survives_newer_unrelated_jobs_and_reaches_curation(
    state, monkeypatch
):
    learning = learner(state, FakeProvider())
    learning.clock = FakeClock()
    project = await publish(state, learning, "projects/heybuddy.md", "HeyBuddy 正式采用 MemoryFS。")
    games = await publish(state, learning, "topics/games.md", "我喜欢策略游戏。")
    internship = await publish(state, learning, "projects/internship.md", "我开始准备实习。")
    service = MemoryCurator(learning)
    catalog, _ = service._files()
    comparison = {"projects/heybuddy.md": state[2].read_file("projects/heybuddy.md")["content"]}
    monkeypatch.setattr(service, "_files", lambda: (catalog, comparison))
    provider = FakeProvider(ModelResponse(text='{"changes":[]}'))
    service.model.provider = provider
    assert (await service.run(force=True))["outcome"] == "completed"
    data = json.loads(provider.requests[0][1].text)
    ids = [row["run_id"] for row in data["history"]]
    assert ids == [project] and games not in ids and internship not in ids
    assert data["evidence_by_path"]["projects/heybuddy.md"] == [project]
    assert data["evidence_status"]["projects/heybuddy.md"] == "verified"


async def test_newest_related_correction_precedes_old_decision_and_has_valid_raw_evidence(state):
    learning = learner(state, FakeProvider())
    learning.clock = FakeClock()
    path = "projects/heybuddy.md"
    first = await publish(state, learning, path, "正式决定采用方案 A。")
    latest = await publish(state, learning, path, "明确更正：采用方案 B，不再采用 A。")
    await publish(state, learning, "topics/games.md", "无关的最新聊天。")
    service = MemoryCurator(learning)
    related = service.repository.related_sources([path])[path]
    assert [r["id"] for r in related] == [latest, first]
    records, by_path, status = service._evidence([path])
    assert [s.run_id for _, s in records] == [first, latest]  # prompt remains chronological
    assert by_path[path] == [latest, first] and status[path] == "verified"
    newer = learning.evidence(records)[-1]
    assert "明确更正" in newer["messages"][0]["text"]
    assert newer["run_id"] == latest


@pytest.mark.parametrize("mode", ["missing", "foreign", "hash"])
async def test_unverifiable_related_source_is_not_promoted_to_user_evidence(state, mode):
    db, sessions, memory, _ = state
    learning = learner(state, FakeProvider())
    learning.clock = FakeClock()
    path = "projects/heybuddy.md"
    run = await publish(state, learning, path, "正式决定采用方案 A。")
    job = learning.repository.status()["last"]["id"]
    source = learning.repository.get(job)["sources"]
    if mode == "missing":
        source["runs"][0]["id"] = "missing-run"
    elif mode == "foreign":
        other = db.create_agent("other", "base", agent_id="foreign")
        conversation = db.create_conversation(other.id)
        source["runs"][0]["id"] = seed(db, conversation.id, "其他 Agent 的私人事实")
    else:
        source["runs"][0]["hash"] = "0" * 64
    learning.repository.save(job, "completed", learning.clock.now(), sources=source)
    service = MemoryCurator(learning)
    records, by_path, status = service._evidence([path])
    assert not records and not by_path[path] and status[path] == "unavailable"
    assert run not in by_path[path]


async def test_no_related_source_is_structural_no_op_and_cannot_support_factual_rewrite(state):
    db, sessions, memory, run = state
    index.create(
        memory,
        "projects/heybuddy.md",
        "---\nname: HeyBuddy\ndescription: 架构\n---\n\n- 采用方案 A。\n",
    )
    learning = learner(state, FakeProvider())
    service = MemoryCurator(learning)
    before = memory.read_file("projects/heybuddy.md")["content"]
    service.model.provider = FakeProvider(ModelResponse(text='{"changes":[]}'))
    assert (await service.run(force=True))["outcome"] == "completed"
    assert memory.read_file("projects/heybuddy.md")["content"] == before
    # A fresh Curator job must not reinterpret old Markdown as confirmed user history.
    index.edit(
        memory,
        "projects/heybuddy.md",
        "方案 A",
        "方案 A（仍待确认）",
        expected_hash=memory.read_file("projects/heybuddy.md")["hash"],
    )
    edit = change(
        old="采用方案 A（仍待确认）。",
        new="采用方案 B。",
        kind="reconcile",
        evidence=[
            dict(
                conversation_id=sessions.current.id, run_id=run, sequence=1, quote="回答要先给结论"
            )
        ],
    )
    service.model.provider = FakeProvider(ModelResponse(text=json.dumps({"changes": [edit]})))
    assert (await service.run(force=True))["error_code"] == "curator_evidence_insufficient"


async def test_other_selected_file_evidence_cannot_reconcile_target(state):
    db, sessions, memory, _ = state
    learning = learner(state, FakeProvider())
    learning.clock = FakeClock()
    a = "projects/heybuddy.md"
    b = "projects/internship.md"
    await publish(state, learning, a, "正式决定采用方案 A。")
    unrelated = await publish(state, learning, b, "实习项目采用方案 B。")
    with db._transaction() as sql:
        sequence = sql.execute(
            "SELECT MIN(sequence) FROM messages WHERE run_id=?", (unrelated,)
        ).fetchone()[0]
    edit = change(
        old="正式决定采用方案 A。",
        new="采用方案 B。",
        kind="reconcile",
        evidence=[
            dict(
                conversation_id=sessions.current.id,
                run_id=unrelated,
                sequence=sequence,
                quote="实习项目采用方案 B。",
            )
        ],
    )
    service = MemoryCurator(learning)
    service.model.provider = FakeProvider(ModelResponse(text=json.dumps({"changes": [edit]})))
    assert (await service.run(force=True))["error_code"] == "invalid_evidence"
    assert "正式决定采用方案 A。" in memory.read_file(a)["content"]


async def test_combined_evidence_budget_does_not_authorize_changes_from_partial_history(state):
    learning = learner(state, FakeProvider())
    learning.clock = FakeClock()
    path = "projects/heybuddy.md"
    await publish(state, learning, path, "旧决定：" + "甲" * 900)
    await publish(state, learning, path, "最新更正：" + "乙" * 900)
    learning.policy = learning.policy.model_copy(update={"source_bytes": 4096})
    records, by_path, status = MemoryCurator(learning)._evidence([path])
    assert len(records) == 1
    assert sum(s.payload_bytes for _, s in records) <= 4096
    assert status[path] == "unavailable"
    assert by_path[path] == [records[0][1].run_id]
