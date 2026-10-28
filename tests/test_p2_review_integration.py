"""Multi-day learning, partial publication, real reopen, curation and P3-aware retrieval."""

import asyncio
import json
from datetime import timedelta

import pytest

from personal_agent.character.context import CharacterContextCompiler
from personal_agent.character.loader import CharacterLoader
from personal_agent.character.storage import CharacterStateRepository
from personal_agent.core.agent import Agent
from personal_agent.core.budget import ContextBudget
from personal_agent.core.models import ModelResponse
from personal_agent.learning.curator import MemoryCurator
from personal_agent.learning.service import MemoryLearningService
from personal_agent.memory import index
from personal_agent.memory.store import MemoryFS
from personal_agent.recall.service import Recall
from personal_agent.reflection.service import ReflectionService
from personal_agent.retrieval import ContextRetriever
from personal_agent.sessions import SessionManager
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.learning import LearningRepository
from personal_agent.storage.reflection import ReflectionRepository
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.storage.threads import ThreadRepository
from personal_agent.tools.builtin import create_builtin_registry
from tests.fakes import FakeProvider
from tests.test_learning import script
from tests.test_memory_retrieval import references
from tests.test_organization_schedule import FakeClock
from tests.test_reflection import candidate, seed


async def test_multi_day_correction_survives_partial_publication_reopen_and_curation(
    tmp_path, monkeypatch
):
    path = tmp_path / "review.db"
    memory = MemoryFS.for_agent(tmp_path / "memory", "heybuddy")
    memory.initialize()
    initial_persona = memory.read().blocks["persona.md"]
    clock = FakeClock()
    package = CharacterLoader().load("sherlock_bbc")
    budget = ContextBudget(context_window=131072)
    topic = "projects/heybuddy.md"
    old_preference = "回答先给结论，不要固定行动清单。"
    new_preference = "回答可按问题详细展开，但仍不要固定行动清单。"

    def service(db, provider):
        return MemoryLearningService(
            LearningRepository(EpisodeRepository(db, "heybuddy")),
            ReflectionService(ReflectionRepository(db, "heybuddy"), memory, provider, budget),
            clock=clock,
        )

    with SQLiteStore(path) as db:
        db.initialize()
        sessions = SessionManager(db)

        def source(text):
            rid = seed(db, sessions.current.id, text)
            stamp = clock.now() - timedelta(seconds=60)
            with db._transaction() as sql:
                sql.execute(
                    "UPDATE runs SET started_at=?,ended_at=? WHERE id=?",
                    (stamp.isoformat(), (stamp + timedelta(seconds=1)).isoformat(), rid),
                )
                sequence = sql.execute(
                    "SELECT MIN(sequence) FROM messages WHERE run_id=?", (rid,)
                ).fetchone()[0]
            return rid, sequence

        learning = service(db, FakeProvider())
        first_text = "请记住：" + old_preference + "项目正式采用方案 A。"
        first, first_sequence = source(first_text)
        groups = [{"path": "human.md", "candidate_ids": [1]}, {"path": topic, "candidate_ids": [2]}]
        learning.model.provider = script(
            [
                candidate(sessions.current.id, first, sequence=first_sequence, quote=first_text),
                candidate(
                    sessions.current.id,
                    first,
                    sequence=first_sequence,
                    quote=first_text,
                    category="decision",
                    subject="HeyBuddy",
                    content="采用方案 A",
                ),
            ],
            groups,
            [
                {
                    "action": "update",
                    "old_text": "# Human\n",
                    "new_text": "# Human\n" + old_preference + "\n",
                    "reason": "明确偏好",
                },
                {
                    "action": "create",
                    "new_text": (
                        "---\nname: HeyBuddy\ndescription: 项目决策\n---\n\n- 采用方案 A。\n"
                    ),
                    "reason": "明确项目决定",
                },
            ],
        )
        assert (await learning.learn(run_ids=[first]))["updated_files"] == 2
        clock.advance(86400)
        repeated, repeated_sequence = source("请记住：" + old_preference)
        learning.model.provider = script(
            [
                candidate(
                    sessions.current.id,
                    repeated,
                    sequence=repeated_sequence,
                    quote="请记住：" + old_preference,
                )
            ],
            [{"path": "human.md", "candidate_ids": [1]}],
            [{"action": "no_op", "reason": "没有新事实"}],
        )
        assert (await learning.learn(run_ids=[repeated]))["updated_files"] == 0
        clock.advance(86400)
        correction_text = "明确更正：" + new_preference + "项目改用方案 B。"
        corrected, corrected_sequence = source(correction_text)
        candidates = [
            candidate(
                sessions.current.id,
                corrected,
                sequence=corrected_sequence,
                quote=correction_text,
                content=new_preference,
            ),
            candidate(
                sessions.current.id,
                corrected,
                sequence=corrected_sequence,
                quote=correction_text,
                category="correction",
                subject="HeyBuddy",
                content="改用方案 B",
            ),
        ]
        learning.model.provider = script(
            candidates,
            groups,
            [
                {
                    "action": "update",
                    "old_text": old_preference,
                    "new_text": new_preference,
                    "reason": "用户纠正",
                },
                {
                    "action": "update",
                    "old_text": "采用方案 A。",
                    "new_text": "采用方案 B。",
                    "reason": "项目纠正",
                },
            ],
        )
        import personal_agent.learning.service as module

        original = module.write_item

        def after_first(*args):
            original(*args)
            asyncio.current_task().cancel()

        with monkeypatch.context() as patch:
            patch.setattr(module, "write_item", after_first)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.create_task(learning.learn(run_ids=[corrected]))
        jid = learning.repository.status()["last"]["id"]
        assert learning.repository.get(jid)["state"] == "requires_reconciliation"
        assert new_preference in memory.read().blocks["human.md"]
        assert "方案 A" in memory.read_file(topic)["content"]
        old_journal = db.load_messages(sessions.current.id)
        human_mtime = memory.path("human.md").stat().st_mtime_ns

    # A new SQLite connection and service recover, without any manual reconcile/retry command.
    with SQLiteStore(path) as db:
        db.initialize()
        sessions = SessionManager(db, memory=memory, budget=budget)
        provider = script(
            candidates,
            groups,
            [
                {"action": "no_op", "reason": "已保存的人类偏好不可重复发布"},
                {
                    "action": "update",
                    "old_text": "采用方案 A。",
                    "new_text": "采用方案 B。",
                    "reason": "剩余更新",
                },
            ],
        )
        learning = service(db, provider)
        assert not provider.requests and learning.repository.get(jid)["state"] == "pending"
        assert (await learning.learn_priority())["outcome"] == "completed"
        assert memory.path("human.md").stat().st_mtime_ns == human_mtime
        assert memory.read().blocks["human.md"].count(new_preference) == 1
        assert "方案 A" not in memory.read_file(topic)["content"]
        assert "方案 B" in memory.read_file(topic)["content"]
        assert len(learning.repository.get(jid)["operations"]) == 2
        clock.advance(86400)
        curator_provider = FakeProvider(ModelResponse(text='{"changes":[]}'))
        learning.model.provider = curator_provider
        curator = MemoryCurator(learning)
        assert (await curator.run(force=True))["outcome"] == "completed"
        data = json.loads(curator_provider.requests[0][1].text)
        assert corrected in data["evidence_by_path"][topic]
        assert first in data["evidence_by_path"][topic]
        clock.advance(86400)
        roles = CharacterStateRepository(
            EpisodeRepository(db, "heybuddy"), package.character_id, package.fingerprint
        )
        roles.initialize()
        sessions.character = CharacterContextCompiler(
            package, on_run=roles.tag_run, source_scope=roles.references
        )
        sessions.retriever = ContextRetriever(ThreadRepository(db, "heybuddy"), memory=memory)
        main = FakeProvider(ModelResponse(text="项目采用方案 B，按需要展开但不列固定行动清单。"))
        agent = Agent(main, create_builtin_registry(), context=sessions.context())
        assert (await agent.run("还记得我的 HeyBuddy 项目和回复偏好吗？")).status == "completed"
        request = main.requests[0]
        assert "character_identity" in request[0].text
        assert new_preference in " ".join(getattr(m, "text", "") for m in request)
        assert any("方案 B" in str(r.get("content", "")) for r in references(request))
        assert memory.read().blocks["persona.md"] == initial_persona
        assert CharacterLoader().load("sherlock_bbc") == package
        assert len(main.requests) == 1
        assert Recall(db, "heybuddy").search("方案 A").hits
        assert db.load_messages(sessions.current.id)[: len(old_journal)] == old_journal
        assert learning.reflection.repository.progress(sessions.current.id) == 0
        assert index.inspect(memory)["ok"]
