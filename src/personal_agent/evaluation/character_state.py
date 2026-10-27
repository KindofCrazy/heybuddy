"""Explicit paid smoke on a new, wholly synthetic SQLite/Memory scope."""

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from openai import AsyncOpenAI

from personal_agent.character.loader import CharacterLoader
from personal_agent.character.models import StatePolicy
from personal_agent.character.state import STATE_PROMPT, CharacterStateService
from personal_agent.character.storage import CharacterStateRepository
from personal_agent.config import load_settings
from personal_agent.core.models import AssistantMessage, RunResult, TextMessage
from personal_agent.evaluation.character import Trace
from personal_agent.evaluation.runner import Budget
from personal_agent.llm.openai_provider import OpenAIProvider
from personal_agent.memory.privacy import require_private_directory
from personal_agent.memory.store import MemoryFS
from personal_agent.organization.episodes import EpisodeOrganizer
from personal_agent.organization.models import OrganizationPolicy
from personal_agent.organization.provider import OrganizerModel
from personal_agent.reflection.service import ReflectionService
from personal_agent.sessions import SessionManager
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.reflection import ReflectionRepository
from personal_agent.storage.sqlite import SQLiteStore


class Clock:
    def __init__(self):
        self.time = datetime(2026, 10, 28, 2, tzinfo=UTC)
        self.ticks = 0

    def now(self):
        return self.time

    def monotonic(self):
        return self.ticks

    def advance(self, seconds):
        self.time += timedelta(seconds=seconds)
        self.ticks += seconds


async def run():
    settings = load_settings()
    folder = Path(".heybuddy/evaluations") / ("p3-state-live-" + uuid4().hex[:12])
    require_private_directory(folder)
    folder.mkdir(parents=True)
    package = CharacterLoader().load("sherlock_bbc")
    clock = Clock()
    budget = Budget(requests=4, tokens=200000)
    async with AsyncOpenAI(
        api_key=settings.api_key.get_secret_value(),
        base_url=str(settings.base_url) if settings.base_url else None,
        max_retries=0,
        timeout=settings.request_timeout_seconds,
    ) as client:
        traced = Trace(OpenAIProvider(client, settings.model), budget)
        with SQLiteStore(folder / "synthetic.db") as db:
            db.initialize()
            sessions = SessionManager(db)
            memory = MemoryFS.for_agent(folder / "memory", sessions.agent.id)
            memory.initialize()
            before = memory.read()
            episodes = EpisodeRepository(db, sessions.agent.id)
            repository = CharacterStateRepository(
                episodes, package.character_id, package.fingerprint
            )
            verifier = ReflectionService(
                ReflectionRepository(db, sessions.agent.id),
                memory,
                traced,
                settings.context_budget(),
            )
            actor = CharacterStateService(
                repository, package, verifier, settings.context_budget(), StatePolicy(), clock
            )
            organizer = EpisodeOrganizer(
                episodes,
                OrganizerModel(
                    traced, settings.context_budget(), OrganizationPolicy(output_tokens=4096)
                ),
            )
            results = []
            for text in (
                "这个单调性假设被我构造的最小反例推翻了；请先核对，不要急着改结论。",
                "检验完成，是我交换了两列导致的伪反例；这个问题结束了，不要继续追问。",
            ):
                run_id = db.begin_run(sessions.current.id, TextMessage(role="user", text=text))
                repository.tag_run(run_id)
                db.append_message(
                    run_id, AssistantMessage(text="合成历史应答：按用户证据核验，不执行外部动作。")
                )
                db.finish_run(run_id, RunResult(status="completed", rounds=1))
                at = clock.now() - timedelta(seconds=60)
                with db._transaction() as sql:
                    sql.execute(
                        "UPDATE runs SET started_at=?,ended_at=? WHERE id=?",
                        (at.isoformat(), (at + timedelta(seconds=1)).isoformat(), run_id),
                    )
                organized = await organizer.organize(sessions.current.id, at=clock.now())
                state_result = await actor.run(force=True)
                results.append(
                    {"organizer": organized, "state": state_result, "active": actor.active()}
                )
                clock.advance(1800)
            before_expiry = actor.active()
            calls = len(traced.calls)
            duplicate = await actor.run(force=True)
            no_duplicate_call = len(traced.calls) == calls
            clock.advance(10801)
            expired = actor.active() is None
            checks = {
                "no_duplicate_call": no_duplicate_call,
                "expired_without_call": expired,
                "memory_unchanged": memory.read() == before,
                "empty_tools": all(not c["tools"] for c in traced.calls),
                "state_updates_verified": all(r["state"].get("updated") for r in results),
            }
            private = {
                "model": settings.model,
                "results": results,
                "duplicate": duplicate,
                "before_expiry": before_expiry,
                "checks": checks,
                "calls": traced.calls,
                "usage": budget.report(),
                "stage_requests": {
                    "state": sum(c["messages"][0]["text"] == STATE_PROMPT for c in traced.calls),
                    "episode": sum(c["messages"][0]["text"] != STATE_PROMPT for c in traced.calls),
                },
            }
            (folder / "private-review.json").write_text(
                json.dumps(private, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                json.dumps(
                    {
                        "output": str(folder.resolve()),
                        "checks": checks,
                        "usage": budget.report(),
                        "state_requests": private["stage_requests"],
                    },
                    ensure_ascii=False,
                )
            )
            return 0 if all(checks.values()) else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="P3 real Episode/State synthetic smoke, 4 calls max"
    )
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        parser.error("Explicit --live required")
    try:
        return asyncio.run(run())
    except Exception as error:
        print(json.dumps({"outcome": "failed", "error_type": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
