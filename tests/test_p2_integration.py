"""Synthetic multi-day runs exercise the real P1→P2→Context pipeline offline."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from personal_agent.core.agent import Agent
from personal_agent.core.budget import ContextBudget
from personal_agent.core.models import ModelResponse
from personal_agent.learning.models import LearningPolicy
from personal_agent.learning.service import LEARN_EXTRACT, LEARN_SELECT, MemoryLearningService
from personal_agent.memory import index
from personal_agent.memory.store import MemoryFS
from personal_agent.organization.runtime import OrganizationRuntime
from personal_agent.recall.service import Recall
from personal_agent.reflection.service import ReflectionService
from personal_agent.retrieval import ContextRetriever
from personal_agent.sessions import SessionManager
from personal_agent.storage import sqlite as storage
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.learning import LearningRepository
from personal_agent.storage.reflection import ReflectionRepository
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.storage.threads import ThreadRepository
from personal_agent.tools.builtin import create_builtin_registry
from tests.fakes import FakeProvider
from tests.test_episodes import organizer, seed
from tests.test_organization_schedule import FakeClock, pump
from tests.test_threads import thread_service


class SyntheticLearningProvider(FakeProvider):
    """Deterministic semantic script, not a scorer or a substitute for live model evaluation."""

    def __init__(self, project=False):
        super().__init__()
        self.project = project
        self.path = "projects/heybuddy.md" if project else "people/career.md"

    async def generate(self, messages, tools, *, max_output_tokens=None):
        data = json.loads(messages[1].text)
        if messages[0].text == LEARN_EXTRACT:
            candidates = []
            for turn in data["history"]:
                for message in turn["messages"]:
                    if message["role"] != "user":
                        continue
                    text = message["text"]
                    candidates.append(
                        dict(
                            category="decision" if self.project else "current_state",
                            subject="HeyBuddy" if self.project else "实习进展",
                            content=text,
                            reason="合成脚本中明确的用户状态",
                            confidence="confirmed",
                            evidence=[
                                dict(
                                    conversation_id=turn["conversation_id"],
                                    run_id=turn["run_id"],
                                    sequence=message["sequence"],
                                    quote=text,
                                )
                            ],
                        )
                    )
            output = {"candidates": candidates}
        elif messages[0].text == LEARN_SELECT:
            output = {
                "groups": [
                    {
                        "path": self.path,
                        "candidate_ids": [c["candidate_id"] for c in data["candidates"]],
                    }
                ]
            }
        else:
            old = data["memory"].get(self.path)
            statements = [c["content"] for c in data["candidates"]]
            if self.project:
                previous = old.split("# 当前认识\n", 1)[1] if old else ""
                facts = previous.splitlines()
                facts = list(dict.fromkeys([*facts, *["- " + s for s in statements]]))
                body = "# 当前认识\n" + "\n".join(facts) + "\n"
            else:
                body = "# 当前认识\n- " + statements[-1] + "\n"
            if old:
                old_body = "# 当前认识\n" + old.split("# 当前认识\n", 1)[1]
                output = {
                    "action": "update",
                    "old_text": old_body,
                    "new_text": body,
                    "reason": "替换过时阶段或合并同项目已确认决定",
                }
            else:
                title = "HeyBuddy" if self.project else "实习进展"
                output = {
                    "action": "create",
                    "new_text": f"---\nname: {title}\n"
                    f"description: {title}的当前认识\n---\n\n" + body,
                    "reason": "新主题",
                }
        self.responses.append(ModelResponse(text=json.dumps(output, ensure_ascii=False)))
        return await super().generate(messages, tools, max_output_tokens=max_output_tokens)


@pytest.mark.parametrize("project", [False, True])
async def test_multi_day_automatic_learning_and_future_answer(tmp_path, project):
    budget = ContextBudget(context_window=131072)
    with SQLiteStore(tmp_path / "multi-day.db") as db:
        db.initialize()
        sessions = SessionManager(db, budget=budget)
        memory = MemoryFS.for_agent(tmp_path / "memory", "heybuddy")
        memory.initialize()
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        provider = SyntheticLearningProvider(project)
        reflection = ReflectionService(
            ReflectionRepository(db, "heybuddy"), memory, provider, budget
        )
        learning = MemoryLearningService(
            LearningRepository(EpisodeRepository(db, "heybuddy")),
            reflection,
            LearningPolicy(batch_episodes=1, max_wait_seconds=1, priority_enabled=False),
            clock,
        )
        episodes = organizer(learning.repository.episodes)
        threads = thread_service(db) if project else None
        runtime = OrganizationRuntime(episodes, learning=learning, threads=threads, clock=clock)
        statements = (
            [
                "正式决定 HeyBuddy 使用 Episode 架构。",
                "正式决定 HeyBuddy 使用 Thread 架构。",
                "正式决定 HeyBuddy 使用 MemoryFS。",
            ]
            if project
            else ["我正在准备算法实习。", "我下周有算法实习面试。", "我已经获得算法实习 Offer。"]
        )
        original_runs = []
        try:
            for day, text in enumerate(statements):
                await runtime.foreground()
                original_runs.append(seed(db, sessions.current.id, text, at=clock.now()))
                runtime.schedule(sessions.current.id)
                await pump()
                clock.advance(14401)
                await pump()
                for _ in range(8):
                    clock.advance(61)
                    await pump()
                    if (
                        memory.path(provider.path).exists()
                        and text in memory.read_file(provider.path)["content"]
                    ):
                        break
                assert memory.path(provider.path).exists(), runtime.last_report
                assert text in memory.read_file(provider.path)["content"], runtime.last_report
                tomorrow = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day + 1)
                clock.advance((tomorrow - clock.now()).total_seconds())
            await runtime.foreground()
            final = memory.read_file(provider.path)["content"]
            evidence = json.loads(provider.requests[0][1].text)
            assert evidence["history"][0]["started_at"].startswith("2026-01-01")
            assert "as_of" in evidence
            assert len(episodes.repository.list(sessions.current.id)) == 3
            assert all(
                e.status == "finalized" for e in episodes.repository.list(sessions.current.id)
            )
            if project:
                assert all(s in final for s in statements)
                assert len(threads.repository.list()) == 1
            else:
                assert "已经获得" in final and "正在准备" not in final and "下周" not in final

            class AnswerProvider(FakeProvider):
                async def generate(self, messages, tools, **kwargs):
                    refs = [
                        json.loads(m.text)["source"]
                        for m in messages
                        if m.text.startswith('{"kind": "working_context_reference"')
                    ]
                    content = " ".join(str(ref.get("content", "")) for ref in refs)
                    assert ("MemoryFS" if project else "已经获得算法实习 Offer") in content
                    self.responses.append(
                        ModelResponse(
                            text="已确认 MemoryFS。" if project else "你已经获得算法实习 Offer。"
                        )
                    )
                    return await super().generate(messages, tools, **kwargs)

            answer = AnswerProvider()
            sessions.memory = memory
            sessions.retriever = ContextRetriever(ThreadRepository(db, "heybuddy"), memory=memory)
            agent = Agent(answer, create_builtin_registry(), context=sessions.context())
            result = await agent.run(
                "继续之前 HeyBuddy 的 Memory Learning 讨论。"
                if project
                else "你还记得我实习的进展吗？"
            )
            assert result.status == "completed"
            assert len(db.list_runs(sessions.current.id)) == 4
            assert len(db.load_messages(sessions.current.id)) == 8
            if not project:
                assert Recall(db, "heybuddy").search("准备算法实习").hits
            assert all(
                db.list_runs(sessions.current.id)[n].id == rid
                for n, rid in enumerate(original_runs)
            )
            assert index.inspect(memory)["ok"]
            assert reflection.repository.progress(sessions.current.id) == 0
        finally:
            await runtime.close()


def test_schema_10_to_11_preserves_journal_and_rolls_back_failed_migration(tmp_path, monkeypatch):
    path = tmp_path / "upgrade.db"
    with SQLiteStore(path) as db:
        monkeypatch.setattr(storage, "SCHEMA_VERSION", 10)
        db.initialize()
        sessions = SessionManager(db)
        seed(db, sessions.current.id, "旧版本历史仍应完整保留")
        before = db.load_messages(sessions.current.id)
        monkeypatch.setattr(storage, "SCHEMA_VERSION", 11)
        original = storage.MIGRATIONS[11]
        monkeypatch.setitem(storage.MIGRATIONS, 11, (*original, "INVALID SQL"))
        from personal_agent.core.history import HistoryError

        with pytest.raises(HistoryError):
            db.initialize()
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
            assert not connection.execute(
                "SELECT name FROM sqlite_master WHERE name='memory_learning_jobs'"
            ).fetchall()
        monkeypatch.setitem(storage.MIGRATIONS, 11, original)
        db.initialize()
        assert db.load_messages(sessions.current.id) == before
