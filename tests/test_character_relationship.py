import json

from personal_agent.character.context import CharacterContextCompiler
from personal_agent.character.loader import CharacterLoader
from personal_agent.character.storage import CharacterStateRepository
from personal_agent.core.agent import Agent
from personal_agent.core.budget import ContextBudget
from personal_agent.core.context import ContextBuilder
from personal_agent.core.models import ModelResponse
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.tools.builtin import create_builtin_registry
from tests.fakes import FakeProvider
from tests.test_learning import human_write, learner, script
from tests.test_reflection import candidate, seed, state

__all__ = ["state"]


async def test_learned_preference_and_correction_reach_character_without_identity_write(state):
    db, sessions, memory, _ = state
    package = CharacterLoader().load("sherlock_bbc")
    before = memory.read().blocks["persona.md"]
    compiler = CharacterContextCompiler(package)
    sessions.character = compiler
    for preference in (
        "不要给我选择菜单，先说结论。",
        "更正：科研讨论可以展开，但不要固定行动清单。",
    ):
        run = seed(db, sessions.current.id, preference)
        with db._transaction() as connection:
            sequence = connection.execute(
                "SELECT MIN(sequence) FROM messages WHERE run_id=?", (run,)
            ).fetchone()[0]
        c = candidate(
            sessions.current.id, run, sequence=sequence, quote=preference, content=preference
        )
        old = memory.read().blocks["human.md"]
        decision = human_write(preference)
        decision["old_text"] = old
        provider = script([c], [{"path": "human.md", "candidate_ids": [1]}], [decision])
        result = await learner((db, sessions, memory, run), provider).learn(run_ids=[run])
        assert result.get("updated_files") == 1, result
        main = FakeProvider(ModelResponse(text="先核对实验假设。"))
        context = ContextBuilder(
            memory=memory, character=compiler, budget=ContextBudget(context_window=131072)
        )
        assert (
            await Agent(main, create_builtin_registry(), context=context).run("继续讨论")
        ).status == "completed"
        assert preference in " ".join(m.text for m in main.requests[0])
        assert len(main.requests) == 1
    assert memory.read().blocks["persona.md"] == before
    assert CharacterLoader().load("sherlock_bbc") == package


async def test_actual_recent_runs_have_bounded_role_attribution_not_canonical_shared_history(state):
    db, sessions, memory, legacy_run = state
    package = CharacterLoader().load("sherlock_bbc")
    repo = CharacterStateRepository(
        EpisodeRepository(db, "heybuddy"), package.character_id, package.fingerprint
    )
    other = CharacterStateRepository(repo.episodes, "other_role", package.fingerprint)
    compiler = CharacterContextCompiler(package, on_run=repo.tag_run, source_scope=repo.references)
    sessions.character = compiler
    provider = FakeProvider(ModelResponse(text="实际工具证据仍需核验。"))
    sessions.memory = memory
    sessions.budget = ContextBudget(context_window=131072)
    agent = Agent(provider, create_builtin_registry(), context=sessions.context())
    assert (await agent.run("我们刚刚调试了索引错误，保留这个讨论。")).status == "completed"
    # Query the actual journal rather than assuming the in-memory page refreshes on finish.
    with db._transaction() as connection:
        tagged = connection.execute(
            "SELECT run_id FROM character_runs WHERE character_id=?", (package.character_id,)
        ).fetchone()[0]
    assert other.references((legacy_run, tagged)) == {
        legacy_run: "unattributed",
        tagged: "other_character",
    }
    assert repo.references(("nonexistent-or-foreign",)) == {}
    provider.responses.append(ModelResponse(text="记录显示我们调试过索引。"))
    assert (await agent.run("记得索引调试吗？你在贝克街认识我吗？")).status == "completed"
    reference = next(
        json.loads(m.text)
        for m in provider.requests[-1]
        if m.text.startswith('{"kind": "character_context"')
    )
    assert reference["source_role_participation"][tagged] == "same_character"
    assert "原作背景不代表你与当前用户共同经历过那些事情" in provider.requests[-1][0].text
    assert len(repo.references(tuple([tagged] * 100))) == 1
