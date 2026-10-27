import asyncio
import json
from datetime import timedelta

import pytest

from personal_agent.character.context import CharacterContextCompiler
from personal_agent.character.loader import CharacterLoader
from personal_agent.character.models import StatePolicy
from personal_agent.character.state import CharacterStateService
from personal_agent.character.storage import CharacterStateRepository
from personal_agent.core.budget import ContextBudget
from personal_agent.core.models import AssistantMessage, ModelResponse, RunResult, TextMessage
from personal_agent.memory.store import MemoryFS
from personal_agent.organization.runtime import OrganizationRuntime
from personal_agent.reflection.service import ReflectionService
from personal_agent.sessions import SessionManager
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.reflection import ReflectionRepository
from personal_agent.storage.sqlite import SQLiteStore
from tests.fakes import FakeProvider
from tests.test_episodes import organizer
from tests.test_organization_schedule import FakeClock


class StateProvider(FakeProvider):
    def __init__(self, *, no_op=False, mutate=None):
        super().__init__()
        self.no_op = no_op
        self.mutate = mutate

    async def generate(self, messages, tools, **kwargs):
        data = json.loads(messages[1].text)
        turn = data["history"][-1]
        user = next(m for m in turn["messages"] if m["role"] == "user")
        output = (
            {"action": "no_op"}
            if self.no_op
            else dict(
                action="update",
                mood="curious",
                current_focus="研究反例",
                stance="open_to_evidence",
                reason="用户提供了新的研究反例。",
                evidence=[
                    dict(
                        conversation_id=turn["conversation_id"],
                        run_id=turn["run_id"],
                        sequence=user["sequence"],
                        quote=user["text"],
                    )
                ],
            )
        )
        if self.mutate:
            self.mutate(output, data)
        self.responses.append(ModelResponse(text=json.dumps(output, ensure_ascii=False)))
        return await super().generate(messages, tools, **kwargs)


def service(db, memory, provider, clock, *, character_id="sherlock_bbc", **policy):
    package = (
        CharacterLoader().load("sherlock_bbc").model_copy(update={"character_id": character_id})
    )
    repository = CharacterStateRepository(
        EpisodeRepository(db, "heybuddy"), character_id, package.fingerprint
    )
    verifier = ReflectionService(
        ReflectionRepository(db, "heybuddy"), memory, provider, ContextBudget(context_window=131072)
    )
    return CharacterStateService(
        repository,
        package,
        verifier,
        ContextBudget(context_window=131072),
        StatePolicy(min_interval_seconds=0, **policy),
        clock,
    )


@pytest.fixture
def state(tmp_path):
    with SQLiteStore(tmp_path / "state.db") as db:
        db.initialize()
        sessions = SessionManager(db)
        memory = MemoryFS.for_agent(tmp_path / "memory", "heybuddy")
        memory.initialize()
        clock = FakeClock()
        provider = StateProvider()
        actor = service(db, memory, provider, clock)
        yield db, sessions, memory, clock, provider, actor


def tagged_run(db, stream, actor, clock, text="这个反例推翻了之前的假设。", *, age=60):
    rid = db.begin_run(stream, TextMessage(role="user", text=text))
    actor.repository.tag_run(rid)
    db.append_message(rid, AssistantMessage(text="先核对证据和适用条件。"))
    db.finish_run(rid, RunResult(status="completed", rounds=1))
    at = clock.now() - timedelta(seconds=age)
    with db._transaction() as connection:
        connection.execute(
            "UPDATE runs SET started_at=?,ended_at=? WHERE id=?",
            (at.isoformat(), (at + timedelta(seconds=1)).isoformat(), rid),
        )
    return rid


async def episode(state):
    db, sessions, memory, clock, provider, actor = state
    run = tagged_run(db, sessions.current.id, actor, clock)
    episodes = organizer(actor.repository.episodes)
    assert (await episodes.organize(sessions.current.id, at=clock.now()))["outcome"] == "completed"
    return run, episodes


async def test_state_is_character_specific_verified_transient_context(state):
    db, sessions, memory, clock, provider, actor = state
    run, _ = await episode(state)
    result = await actor.run(force=True)
    assert result["updated"] and actor.repository.current().source_run_id == run
    view = actor.active()
    assert view["mood"] == "curious" and view["character_id"] == "sherlock_bbc"
    compiler = CharacterContextCompiler(actor.package, state=actor.active)
    assert "transient_simulated_state" in compiler.compile("科研反例")[1].text
    assert len(provider.requests) == 1 and provider.tools == [[]]
    prompt = json.loads(provider.requests[0][1].text)
    assert prompt["identity"] == actor.package.character
    assert prompt["behavior"] == actor.package.behavior
    assert "source_run_id" not in memory.compile()


async def test_no_new_events_and_finalization_do_not_repeat_paid_update(state):
    db, sessions, memory, clock, provider, actor = state
    assert (await actor.run(force=True))["outcome"] == "no_character_work"
    _, episodes = await episode(state)
    await actor.run(force=True)
    assert (await actor.run(force=True))["outcome"] == "no_character_work"
    episodes.repository.finalize(
        sessions.current.id, episodes.policy, at=clock.now() + timedelta(hours=4)
    )
    result = await actor.run(force=True)
    assert not result["updated"] and result["requests"] == 0
    assert len(provider.requests) == 1 and actor.repository.current().revision == 1


async def test_no_op_advances_source_without_creating_state(state):
    _, _, _, _, provider, actor = state
    await episode(state)
    provider.no_op = True
    assert not (await actor.run(force=True))["updated"]
    assert actor.repository.current() is None
    assert actor.repository.candidate() is None
    assert actor.status()["counts"] == {"completed": 1}


async def test_mood_and_focus_expire_without_deleting_state_or_history(state):
    db, sessions, memory, clock, provider, actor = state
    await episode(state)
    await actor.run(force=True)
    assert actor.active()["mood"] == "curious"
    before = db.load_messages(sessions.current.id)
    clock.advance(3600, wall_seconds=-3600)
    assert actor.active()["mood"] == "neutral"
    assert actor.active()["current_focus"] == "研究反例"
    clock.advance(10800)
    assert actor.active() is None
    assert actor.repository.current() is not None
    assert db.load_messages(sessions.current.id) == before
    assert len(provider.requests) == 1


async def test_two_characters_only_consume_their_own_tagged_runs(state):
    db, sessions, memory, clock, provider, actor = state
    other_provider = StateProvider()
    other = service(db, memory, other_provider, clock, character_id="another")
    run_a = tagged_run(
        db, sessions.current.id, actor, clock, text="我给 Sherlock 一个新的研究反例。"
    )
    run_b = tagged_run(db, sessions.current.id, other, clock, text="我给另一个角色讲一个独立事件。")
    await organizer(actor.repository.episodes).organize(sessions.current.id, at=clock.now())
    await actor.run(force=True)
    assert actor.repository.current().source_run_id == run_a and other.active() is None
    assert run_b not in provider.requests[0][1].text
    await other.run(force=True)
    assert other.repository.current().source_run_id == run_b
    assert actor.active()["character_id"] != other.active()["character_id"]


async def test_invalid_output_and_forged_evidence_fail_until_explicit_retry(state):
    db, sessions, memory, clock, provider, actor = state
    await episode(state)
    actor.model.provider = FakeProvider(ModelResponse(text="invalid"))
    assert (await actor.run(force=True))["outcome"] == "failed"
    assert actor.repository.candidate() is None and actor.repository.current() is None
    actor.model.provider = StateProvider(
        mutate=lambda output, data: output["evidence"][0].update(quote="这句话没有实际出现在来源里")
    )
    assert (await actor.run(force=True, retry=True))["outcome"] == "failed"
    actor.model.provider = provider
    assert (await actor.run(force=True, retry=True))["updated"]


async def test_cancel_pending_and_resume_without_losing_journal(state):
    db, sessions, memory, clock, provider, actor = state
    await episode(state)
    entered = asyncio.Event()

    class Waiting:
        async def generate(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    actor.model.provider = Waiting()
    before = db.load_messages(sessions.current.id)
    task = asyncio.create_task(actor.run(force=True))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert actor.status()["counts"] == {"pending": 1}
    restored = service(db, memory, provider, clock)
    assert (await restored.run(force=True))["updated"]
    assert db.load_messages(sessions.current.id) == before


async def test_cas_prevents_late_state_overwrite(state):
    db, sessions, memory, clock, provider, actor = state
    await episode(state)

    def change(output, data):
        with db._transaction() as connection:
            connection.execute(
                "UPDATE character_states SET revision=revision+1 "
                "WHERE agent_id='heybuddy' AND character_id='sherlock_bbc'"
            )

    provider.mutate = change
    assert (await actor.run(force=True))["outcome"] == "failed"
    assert actor.repository.current() is None
    assert actor.repository.row()["revision"] == 1


async def test_old_event_is_consumed_without_refreshing_expired_mood(state):
    db, sessions, memory, clock, provider, actor = state
    tagged_run(db, sessions.current.id, actor, clock, age=86400)
    await organizer(actor.repository.episodes).organize(sessions.current.id, at=clock.now())
    result = await actor.run(force=True)
    assert not result["updated"] and not provider.requests


async def test_runtime_state_update_is_passive_budgeted_and_preemptible(state):
    db, sessions, memory, clock, provider, actor = state
    _, episodes = await episode(state)
    clock.advance(1501)
    entered = asyncio.Event()

    class Waiting:
        async def generate(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    actor.model.provider = Waiting()
    runtime = OrganizationRuntime(episodes, character_state=actor, clock=clock)
    try:
        runtime.schedule(sessions.current.id)
        await asyncio.wait_for(entered.wait(), 3)
        assert runtime.current_stage == "character_state"
        await runtime.foreground()
        assert actor.status()["counts"] == {"pending": 1}
        assert runtime.last_report["cycle"]["requests"] == 1
    finally:
        await runtime.close()


async def test_actual_agent_run_tags_its_character_without_journaling_identity(state):
    from personal_agent.core.agent import Agent
    from personal_agent.tools.builtin import create_builtin_registry

    db, sessions, memory, clock, provider, actor = state
    sessions.character = CharacterContextCompiler(actor.package, on_run=actor.repository.tag_run)
    main = FakeProvider(ModelResponse(text="这条结论需要证据。"))
    agent = Agent(main, create_builtin_registry(), context=sessions.context())
    assert (await agent.run("我们讨论一个新的反例。")).status == "completed"
    with db._transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM character_runs").fetchone()[0] == 1
    assert len(db.load_messages(sessions.current.id)) == 2
    assert len(main.requests) == 1 and not provider.requests


async def test_state_restores_from_reopened_sqlite_without_model_calls(state):
    db, sessions, memory, clock, provider, actor = state
    await episode(state)
    await actor.run(force=True)
    before = actor.repository.current()
    path = db.path
    db.close()
    with SQLiteStore(path) as reopened:
        reopened.initialize()
        fresh_provider = FakeProvider()
        restored = service(reopened, memory, fresh_provider, clock)
        assert restored.repository.current() == before
        assert restored.active()["mood"] == "curious"
        assert (await restored.run(force=True))["outcome"] == "no_character_work"
        assert not fresh_provider.requests


async def test_new_package_invalidates_old_state_and_does_not_reinterpret_old_runs(state):
    db, sessions, memory, clock, provider, actor = state
    await episode(state)
    await actor.run(force=True)
    changed = actor.package.model_copy(update={"fingerprint": "b" * 64})
    repo = CharacterStateRepository(
        actor.repository.episodes, changed.character_id, changed.fingerprint
    )
    fresh = CharacterStateService(
        repo, changed, actor.verifier, ContextBudget(context_window=131072), actor.policy, clock
    )
    assert fresh.active() is None and fresh.repository.candidate() is None
    assert fresh.repository.row()["revision"] == 2


async def test_minimum_interval_coalesces_revisions_without_extra_model_calls(state):
    db, sessions, memory, clock, provider, actor = state
    await episode(state)
    actor.policy = StatePolicy(min_interval_seconds=900)
    await actor.run(force=True)
    tagged_run(db, sessions.current.id, actor, clock, text="再加一个新的反例用于检验。", age=10)
    await organizer(actor.repository.episodes).organize(sessions.current.id, at=clock.now())
    assert not actor.ready()
    assert (await actor.run())["outcome"] == "no_character_work"
    assert len(provider.requests) == 1
    clock.advance(901)
    assert actor.ready()
    assert (await actor.run())["updated"]


async def test_old_evidence_in_a_new_revision_does_not_refresh_state(state):
    db, sessions, memory, clock, provider, actor = state
    await episode(state)
    await actor.run(force=True)
    before = actor.repository.current()
    tagged_run(db, sessions.current.id, actor, clock, text="只是多说一句无关内容。", age=5)
    await organizer(actor.repository.episodes).organize(sessions.current.id, at=clock.now())

    def cite_old(output, data):
        turn = data["history"][0]
        user = turn["messages"][0]
        output["evidence"] = [
            dict(
                conversation_id=turn["conversation_id"],
                run_id=turn["run_id"],
                sequence=user["sequence"],
                quote=user["text"],
            )
        ]

    provider.mutate = cite_old
    assert not (await actor.run(force=True))["updated"]
    assert actor.repository.current() == before
