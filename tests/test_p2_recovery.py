"""Actual process exit windows use synthetic stores and a tool-free fake Provider."""

import asyncio
import subprocess
import sys

import pytest

from personal_agent.core.budget import ContextBudget
from personal_agent.learning.service import MemoryLearningService
from personal_agent.memory.store import MemoryFS
from personal_agent.organization.runtime import OrganizationRuntime
from personal_agent.reflection.service import ReflectionService
from personal_agent.sessions import SessionManager
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.learning import LearningRepository
from personal_agent.storage.reflection import ReflectionRepository
from personal_agent.storage.sqlite import SQLiteStore
from tests.test_episodes import organizer
from tests.test_learning import script
from tests.test_organization_schedule import pump


@pytest.mark.parametrize("mode", ["before", "after"])
async def test_actual_process_crash_before_and_after_memory_publication(tmp_path, mode):
    path = tmp_path / "crash.db"
    root = tmp_path / "memory"
    child = r"""
import asyncio, json, os, sys
from pathlib import Path
from personal_agent.core.budget import ContextBudget
from personal_agent.core.models import TextMessage, AssistantMessage, RunResult, ModelResponse
from personal_agent.learning.models import LearningPolicy
from personal_agent.learning.service import MemoryLearningService, LEARN_EXTRACT, LEARN_SELECT
from personal_agent.memory.store import MemoryFS
from personal_agent.reflection.service import ReflectionService
from personal_agent.sessions import SessionManager
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.learning import LearningRepository
from personal_agent.storage.reflection import ReflectionRepository
from personal_agent.storage.sqlite import SQLiteStore
import personal_agent.learning.service as learning_module

class Provider:
    async def generate(self,messages,tools,**kwargs):
        data=json.loads(messages[1].text)
        if messages[0].text==LEARN_EXTRACT:
            if sys.argv[3]=='before': os._exit(41)
            turn=data['history'][0]; message=turn['messages'][0]
            output={'candidates':[{'category':'preference','subject':'reply','content':'concise',
              'reason':'explicit user preference','confidence':'confirmed','evidence':[{
              'conversation_id':turn['conversation_id'],'run_id':turn['run_id'],
              'sequence':message['sequence'],'quote':message['text']}]}]}
        elif messages[0].text==LEARN_SELECT:
            output={'groups':[{'path':'human.md','candidate_ids':[1]}]}
        else:
            output={'action':'update','old_text':'# Human\n',
                    'new_text':'# Human\nConcise replies.\n',
                    'reason':'explicit user preference'}
        return ModelResponse(text=json.dumps(output))

async def run():
    with SQLiteStore(Path(sys.argv[1])) as db:
        db.initialize(); sessions=SessionManager(db)
        memory=MemoryFS.for_agent(Path(sys.argv[2]),'heybuddy'); memory.initialize()
        reflection=ReflectionService(ReflectionRepository(db,'heybuddy'),memory,Provider(),
                                     ContextBudget(context_window=64000))
        learner=MemoryLearningService(LearningRepository(EpisodeRepository(db,'heybuddy')),
                                      reflection,LearningPolicy())
        rid=db.begin_run(sessions.current.id,
                        TextMessage(role='user',text='Remember concise replies.'))
        db.append_message(rid,AssistantMessage(text='Understood.'))
        db.finish_run(rid,RunResult(status='completed',rounds=1))
        original=learning_module.write_item
        def publish_then_die(*args):
            original(*args)
            os._exit(42)
        if sys.argv[3]=='after': learning_module.write_item=publish_then_die
        await learner.learn_priority()
asyncio.run(run())
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-u", "-c", child, str(path), str(root), mode],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == (41 if mode == "before" else 42), result.stderr.decode(
        errors="replace"
    )
    with SQLiteStore(path) as db:
        db.initialize()
        sessions = SessionManager(db)
        memory = MemoryFS.for_agent(root, "heybuddy")
        memory.initialize()
        provider = script([])
        reflection = ReflectionService(
            ReflectionRepository(db, "heybuddy"),
            memory,
            provider,
            ContextBudget(context_window=64000),
        )
        learning = MemoryLearningService(
            LearningRepository(EpisodeRepository(db, "heybuddy")), reflection
        )
        before = db.load_messages(sessions.current.id)
        runtime = OrganizationRuntime(organizer(learning.repository.episodes), learning=learning)
        try:
            runtime.schedule(sessions.current.id)
            await pump()
            with db._transaction() as connection:
                jid = connection.execute("SELECT id FROM memory_learning_jobs").fetchone()[0]
            if mode == "before":
                await pump(lambda: learning.repository.get(jid)["state"] == "completed")
                assert learning.repository.get(jid)["state"] == "completed"
                assert len(provider.requests) == 1
            else:
                assert learning.repository.get(jid)["state"] == "requires_reconciliation"
                assert not provider.requests
                assert "Concise replies" in memory.read().blocks["human.md"]
                assert learning.reconcile(jid)["outcome"] == "completed"
                assert not provider.requests
            assert db.load_messages(sessions.current.id) == before
        finally:
            await runtime.close()
