"""Opt-in P1 smoke on fresh synthetic data. No Memory Apply or real user history."""

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from openai import AsyncOpenAI

from personal_agent.config import Settings, load_settings
from personal_agent.core.agent import Agent
from personal_agent.core.compaction import SUMMARY_PROMPT
from personal_agent.core.context import DEFAULT_SYSTEM_PROMPT
from personal_agent.core.models import (
    AssistantMessage,
    Message,
    ModelResponse,
    RunResult,
    TextMessage,
    ToolDefinition,
)
from personal_agent.evaluation.runner import Budget
from personal_agent.llm.base import Provider, ProviderError
from personal_agent.llm.openai_provider import OpenAIProvider
from personal_agent.memory.privacy import require_private_directory
from personal_agent.memory.store import MemoryFS
from personal_agent.organization.episodes import EPISODE_PROMPT, EpisodeOrganizer
from personal_agent.organization.provider import OrganizerModel
from personal_agent.organization.threads import THREAD_PROMPT, ThreadOrganizer
from personal_agent.recall.service import Recall
from personal_agent.recall.tools import register_recall_tools
from personal_agent.retrieval import ContextRetriever
from personal_agent.sessions import SessionManager
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.storage.threads import ThreadRepository
from personal_agent.tools.builtin import create_builtin_registry

# Frozen synthetic expectations. They are not tuned against model output.
CORPUS = (
    (0, "HeyBuddy P1 采用 SQLite，先实现持续对话，再整理 Episode。", "建议保留 Run Journal。"),
    (180, "确认保留 Run Journal。HeyBuddy 下一步实现 Thread。", "收到，Thread 组织跨天话题。"),
    (240, "换个话题，今天晚饭的面条很好吃。", "晚饭听起来不错。"),
    (36000, "继续 HeyBuddy。更正：下一步先完成 Working Context，然后再讨论 P2。", "收到更正。"),
)


class SmokeProvider:
    def __init__(self, provider: Provider, budget: Budget) -> None:
        self.provider = provider
        self.budget = budget
        self.calls = []

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        max_output_tokens: int | None = None,
    ) -> ModelResponse:
        self.budget.reserve(messages, tools, max_output_tokens)
        stage = {
            EPISODE_PROMPT: "episode",
            THREAD_PROMPT: "thread",
            SUMMARY_PROMPT: "compaction",
        }.get(messages[0].text, "main")
        record = {"stage": stage, "usage": None}
        self.calls.append(record)
        start = perf_counter()
        usage = None
        try:
            response = await self.provider.generate(
                messages, tools, max_output_tokens=max_output_tokens
            )
            usage = response.usage
            # Private file only, for human semantic review. Never include raw reasoning.
            record["text"] = response.text
            return response
        except ProviderError as error:
            usage = error.diagnostics.usage if error.diagnostics else None
            record["error_code"] = error.code
            raise
        finally:
            self.budget.observe(usage)
            record["seconds"] = perf_counter() - start
            record["usage"] = usage.model_dump() if usage else None


def seed(
    db: SQLiteStore, stream: str, user: str, answer: str, stamp: datetime | None = None
) -> str:
    run_id = db.begin_run(stream, TextMessage(role="user", text=user))
    db.append_message(run_id, AssistantMessage(text=answer))
    db.finish_run(run_id, RunResult(status="completed", rounds=1))
    if stamp:
        # Only fixture construction in this newly-created synthetic database.
        with db._transaction() as connection:
            connection.execute(
                "UPDATE runs SET started_at=?,ended_at=? WHERE id=?",
                (stamp.isoformat(), (stamp + timedelta(seconds=1)).isoformat(), run_id),
            )
    return run_id


def prepare_folder(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=False)
    require_private_directory(folder)


async def evaluate(folder: Path, provider: Provider, settings: Settings, budget: Budget) -> dict:
    prepare_folder(folder)
    traced = SmokeProvider(provider, budget)
    checks = {}
    with SQLiteStore(folder / "synthetic.sqlite3") as db:
        db.initialize()
        sessions = SessionManager(
            db,
            budget=settings.context_budget(),
            optional_history_tokens=settings.retrieval_optional_tokens,
        )
        stream = sessions.current.id
        memory = MemoryFS.for_agent(folder / "memory", sessions.agent.id)
        memory.initialize()
        before_memory = memory.read()
        sessions.memory = memory
        stamp = datetime(2025, 6, 1, 15, 55, tzinfo=UTC)  # Local +08:00 crosses midnight.
        ids = [
            seed(db, stream, user, answer, stamp + timedelta(seconds=offset))
            for offset, user, answer in CORPUS
        ]
        episodes = EpisodeRepository(db, sessions.agent.id)
        threads = ThreadRepository(db, sessions.agent.id)
        policy = settings.organization_policy()
        episode_report = await EpisodeOrganizer(
            episodes, OrganizerModel(traced, settings.context_budget(), policy)
        ).organize(stream)
        thread_report = await ThreadOrganizer(
            threads, OrganizerModel(traced, settings.context_budget(), policy)
        ).organize()
        organized = sorted(episodes.list(stream), key=lambda e: e.started_at)
        checks["expected_contiguous_segments"] = [e.run_ids for e in organized] == (
            [ids[:2], [ids[2]], [ids[3]]]
        )
        by_run = {rid: e for e in organized for rid in e.run_ids}
        checks["cross_day_thread_and_unthreaded_smalltalk"] = bool(
            ids[0] in by_run
            and ids[3] in by_run
            and ids[2] in by_run
            and by_run[ids[0]].thread_id
            and by_run[ids[0]].thread_id == by_run[ids[3]].thread_id
            and by_run[ids[2]].thread_id is None
        )
        # A fixed synthetic checkpoint puts old Runs outside the recent window.
        # This is not a model-quality claim about Compaction.
        old_page = db.context_page(stream, 0, max_runs=4)
        db.save_context_summary(
            stream, "合成工作检查点；历史细节请按来源核对。", None, old_page.turns
        )
        for index in range(3):
            seed(db, stream, f"合成近期闲聊 {index}", "收到。")
        before_journal = db.load_messages(stream)
        sessions.retriever = ContextRetriever(threads, settings.retrieval_policy())
        tools = create_builtin_registry()
        register_recall_tools(tools, Recall(db, sessions.agent.id))
        agent = Agent(
            traced,
            tools,
            context=sessions.context(),
            max_rounds=3,
            request_timeout_seconds=settings.request_timeout_seconds,
        )
        result = await agent.run("继续昨天的 HeyBuddy 架构讨论，现在下一步是什么？")
        checks["main_completed"] = result.status == "completed"
        checks["thread_retrieval"] = agent.context.diagnostics()["retrieval_path"] == "thread"
        checks["journal_preserved"] = (
            db.load_messages(stream)[: len(before_journal)] == before_journal
        )
        checks["memory_unchanged_no_apply"] = memory.read() == before_memory
        private = {
            "episodes": [e.model_dump(mode="json") for e in organized],
            "threads": [t.model_dump(mode="json") for t in threads.list()],
            "answer": result.text,
            "calls": traced.calls,
        }
        (folder / "private-review.json").write_text(
            json.dumps(private, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    report = {
        "checks": checks,
        "contract_pass": all(checks.values()),
        "semantic_review_required": True,
        "episode_outcome": episode_report["outcome"],
        "thread_outcome": thread_report["outcome"],
        "corpus_hash": hashlib.sha256(json.dumps(CORPUS, ensure_ascii=False).encode()).hexdigest(),
        "prompt_hashes": {
            stage: hashlib.sha256(prompt.encode()).hexdigest()
            for stage, prompt in (
                ("main", DEFAULT_SYSTEM_PROMPT),
                ("summary", SUMMARY_PROMPT),
                ("episode", EPISODE_PROMPT),
                ("thread", THREAD_PROMPT),
            )
        },
        "usage": budget.report(),
        "stage_requests": {
            stage: sum(c["stage"] == stage for c in traced.calls)
            for stage in ("episode", "thread", "main", "compaction")
        },
        "memory_automation_requests": 0,
    }
    (folder / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="P1 synthetic smoke; real semantics need human review"
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(".heybuddy/evaluations"))
    parser.add_argument("--max-requests", type=int, default=6)
    parser.add_argument("--max-tokens", type=int, default=196608)
    args = parser.parse_args()
    if not args.live:
        parser.error("Use --live to explicitly authorize paid model calls")

    async def run() -> int:
        settings = load_settings()
        budget = Budget(args.max_requests, args.max_tokens)
        async with AsyncOpenAI(
            api_key=settings.api_key.get_secret_value(),
            base_url=str(settings.base_url) if settings.base_url else None,
            max_retries=0,
            timeout=settings.request_timeout_seconds,
        ) as client:
            folder = args.output / ("p1-" + uuid4().hex[:12])
            report = await evaluate(
                folder, OpenAIProvider(client, settings.model), settings, budget
            )
            print(json.dumps(report, ensure_ascii=False))
            return 0 if report["contract_pass"] else 1

    try:
        return asyncio.run(run())
    except Exception as error:
        print(json.dumps({"outcome": "failed", "error_type": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
