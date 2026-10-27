import asyncio
import subprocess
import sys
import threading

import pytest

from personal_agent.automation.service import MemoryAutomation
from personal_agent.config import Settings
from personal_agent.core.agent import Agent
from personal_agent.core.models import ModelResponse
from personal_agent.main import chat
from personal_agent.memory.store import MemoryFS
from personal_agent.organization.runtime import OrganizationRuntime
from personal_agent.reflection.service import ReflectionService
from personal_agent.sessions import SessionManager
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.reflection import ReflectionRepository
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.tools.builtin import create_builtin_registry
from tests.test_episodes import organizer, state

__all__ = ["state"]


@pytest.mark.parametrize("stage", ["before", "after"])
async def test_actual_process_exit_before_and_after_episode_publish(tmp_path, stage):
    path = tmp_path / "crash.db"
    script = r"""
import asyncio, json, os, sys, threading
from pathlib import Path
from personal_agent.core.agent import Agent
from personal_agent.core.budget import ContextBudget
from personal_agent.core.models import ModelResponse
from personal_agent.main import chat
from personal_agent.organization.episodes import EpisodeOrganizer, EPISODE_PROMPT
from personal_agent.organization.models import OrganizationPolicy
from personal_agent.organization.provider import OrganizerModel
from personal_agent.organization.runtime import OrganizationRuntime
from personal_agent.sessions import SessionManager
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.tools.builtin import create_builtin_registry

class Provider:
    async def generate(self, messages, tools, **kwargs):
        if messages[0].text != EPISODE_PROMPT:
            return ModelResponse(text="synthetic completed answer")
        if sys.argv[2] == "before":
            os._exit(23)
        data = json.loads(messages[1].text)
        return ModelResponse(text=json.dumps({"episodes": [{
            "run_ids": [r["run_id"] for r in data["runs"]],
            "title": "Synthetic", "summary": "Synthetic", "highlights": []}]}))

class Repository(EpisodeRepository):
    def publish(self, *args):
        super().publish(*args)
        os._exit(24)

calls = 0
def read(prompt):
    global calls
    calls += 1
    if calls == 1:
        return "synthetic input"
    threading.Event().wait()

async def run():
    with SQLiteStore(Path(sys.argv[1])) as db:
        db.initialize()
        sessions = SessionManager(db)
        provider = Provider()
        organizer = EpisodeOrganizer(Repository(db, sessions.agent.id), OrganizerModel(
            provider, ContextBudget(context_window=64000), OrganizationPolicy(run_threshold=1)))
        runtime = OrganizationRuntime(organizer)
        agent = Agent(provider, create_builtin_registry(), context=sessions.context())
        await chat(agent, read, lambda line: print(line, flush=True), sessions=sessions,
                   organization=runtime)
asyncio.run(run())
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-u", "-c", script, str(path), stage],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == (23 if stage == "before" else 24)
    assert b"synthetic completed answer" in result.stdout
    with SQLiteStore(path) as db:
        db.initialize()
        sessions = SessionManager(db)
        assert db.list_runs(sessions.current.id)[0].status == "completed"
        repo = EpisodeRepository(db, sessions.agent.id)
        repo.recover()
        service = organizer(repo)
        report = await service.organize(sessions.current.id)
        assert report["outcome"] == ("completed" if stage == "before" else "no_history")
        assert len(repo.list(sessions.current.id)) == 1
        assert (await service.organize(sessions.current.id))["outcome"] == "no_history"


async def test_background_memory_cancel_keeps_approval_and_explicit_retry_contract(state):
    db, stream, repo = state
    sessions = SessionManager(db)
    memory = MemoryFS.for_agent(db.path.parent / "memory", sessions.agent.id)
    memory.initialize()
    before = memory.read()
    started = threading.Event()
    calls = 0

    class Provider:
        async def generate(self, messages, tools, **kwargs):
            nonlocal calls
            if "should_reflect" in messages[1].text:
                started.set()
                await asyncio.Event().wait()
            calls += 1
            return ModelResponse(text="answer")

    provider = Provider()
    settings = Settings(api_key="test-key", model="test-model", context_window=64000)
    reflection = ReflectionService(
        ReflectionRepository(db, sessions.agent.id), memory, provider, settings.context_budget()
    )
    automation = MemoryAutomation(reflection)
    runtime = OrganizationRuntime(organizer(repo), automation=automation)
    reads = 0

    def read(prompt):
        nonlocal reads
        reads += 1
        if reads == 1:
            return "回答以后先给结论，这是一条偏好"
        if reads == 2:
            assert started.wait(2)
            return "新的输入必须优先"
        return "quit"

    agent = Agent(provider, create_builtin_registry(), context=sessions.context())
    await asyncio.wait_for(
        chat(
            agent,
            read,
            lambda _: None,
            sessions=sessions,
            automation=automation,
            organization=runtime,
        ),
        5,
    )
    assert calls == 2 and memory.read() == before
    assert all(r.status == "completed" for r in db.list_runs(stream))
    assert automation.repository.active(stream)["state"] == "interrupted"
    assert (await automation.run(stream))["outcome"] == "blocked"


def test_process_can_exit_while_real_console_input_is_pending(tmp_path):
    script = r"""
import asyncio
from personal_agent.core.agent import Agent
from personal_agent.core.budget import ContextBudget
from personal_agent.main import chat
from personal_agent.organization.episodes import EpisodeOrganizer
from personal_agent.organization.models import OrganizationPolicy
from personal_agent.organization.provider import OrganizerModel
from personal_agent.organization.runtime import OrganizationRuntime
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.tools.builtin import create_builtin_registry

class NoCalls:
    async def generate(self, *args, **kwargs):
        raise AssertionError("No network or model requests")

async def run():
    runtime = OrganizationRuntime(EpisodeOrganizer(EpisodeRepository(None, "synthetic"),
        OrganizerModel(NoCalls(), ContextBudget(context_window=64000), OrganizationPolicy())))
    try:
        await asyncio.wait_for(chat(Agent(NoCalls(), create_builtin_registry()),
                                    organization=runtime), 0.15)
    except TimeoutError:
        pass
asyncio.run(run())
"""
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.wait(timeout=8) == 0
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
