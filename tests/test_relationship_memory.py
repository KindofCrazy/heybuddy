"""Evidenced relationship learning stays separate from immutable character identity."""

import json

import pytest

from personal_agent.character.context import CharacterContextCompiler
from personal_agent.character.loader import CharacterLoader
from personal_agent.core.context import ContextBuilder
from personal_agent.core.history import HistoryError
from personal_agent.core.persona import PERSONA_HEADER
from tests.test_learning import learner, script
from tests.test_reflection import candidate, seed, state

__all__ = ["state"]

FEEDBACK = "我们复盘时你先听我说完再分析，这种相处方式很合适。"
REFLECTION = "复盘时先听用户把经历说完，再一起分析，更符合双方已确认的交流方式。"


def setup_learning(state, *, confidence="confirmed", replacement=None):
    db, sessions, memory, _ = state
    run = seed(db, sessions.current.id, FEEDBACK)
    c = candidate(
        sessions.current.id,
        run,
        sequence=3,
        quote=FEEDBACK,
        category="relationship",
        content=REFLECTION,
        confidence=confidence,
    )
    provider = script(
        [c],
        [{"path": "persona.md", "candidate_ids": [1]}],
        [
            {
                "action": "update",
                "old_text": memory.read().blocks["persona.md"],
                "new_text": replacement or PERSONA_HEADER + "\n" + REFLECTION + "\n",
                "reason": "用户确认了实际交流中合适的相处方式",
            }
        ],
    )
    return learner(state, provider), provider, run


async def test_background_relationship_is_injected_as_data_without_changing_character(state):
    memory = state[2]
    package = CharacterLoader().load("sherlock_bbc")
    compiler = CharacterContextCompiler(package)
    before_system = compiler.compile("继续聊")[0]
    learning, provider, run = setup_learning(state)
    assert (await learning.learn(run_ids=[run]))["updated_files"] == 1
    context = ContextBuilder(memory=memory, character=compiler)
    messages = context.for_request(context.build("继续聊"))
    core = next(json.loads(m.text) for m in messages if '"kind": "agent_core_memory"' in m.text)
    assert REFLECTION in core["blocks"]["persona.md"]
    assert core["persona_semantics"] == "relationship_memory_not_character_identity"
    assert messages[0] == before_system
    assert REFLECTION not in messages[0].text
    assert CharacterLoader().load("sherlock_bbc") == package
    assert json.loads(provider.requests[1][1].text)["relationship_memory_available"] is True
    assert (await learning.learn(run_ids=[run]))["outcome"] == "completed"
    assert len(provider.requests) == 3


@pytest.mark.parametrize("original", ["# Persona\n", PERSONA_HEADER])
async def test_empty_legacy_template_and_new_template_accept_first_relationship(state, original):
    memory = state[2]
    memory.update("persona.md", original, expected_hash=memory.read().hashes["persona.md"])
    learning, _, run = setup_learning(state)
    assert (await learning.learn(run_ids=[run]))["updated_files"] == 1
    assert memory.read().blocks["persona.md"].startswith(PERSONA_HEADER)


async def test_nonempty_legacy_persona_is_preserved_and_not_reinterpreted(state):
    memory = state[2]
    legacy = "# Persona\n旧的固定身份与人格设定。\n"
    memory.update("persona.md", legacy, expected_hash=memory.read().hashes["persona.md"])
    memory.initialize()
    learning, provider, run = setup_learning(state)
    result = await learning.learn(run_ids=[run])
    assert result["error_code"] == "learning_relationship_target_forbidden"
    assert len(provider.requests) == 2
    assert json.loads(provider.requests[1][1].text)["relationship_memory_available"] is False
    assert memory.read().blocks["persona.md"] == legacy


@pytest.mark.parametrize("mode", ["uncertain", "identity_header"])
async def test_uncertainty_or_identity_replacement_cannot_publish_relationship(state, mode):
    before = state[2].read()
    learning, _, run = setup_learning(
        state,
        confidence="uncertain" if mode == "uncertain" else "confirmed",
        replacement="# Persona\n把相处记忆改回固定身份。\n" if mode == "identity_header" else None,
    )
    result = await learning.learn(run_ids=[run])
    assert result["outcome"] == ("completed" if mode == "uncertain" else "failed")
    assert state[2].read() == before


async def test_relationship_publication_receipt_can_reconcile_without_rewriting(state, monkeypatch):
    memory = state[2]
    learning, provider, run = setup_learning(state)
    save = learning.repository.save

    def fail_receipt(jid, status, now, **kwargs):
        if any(op["state"] == "applied" for op in kwargs.get("operations", [])):
            raise HistoryError("simulated receipt failure")
        return save(jid, status, now, **kwargs)

    monkeypatch.setattr(learning.repository, "save", fail_receipt)
    with pytest.raises(HistoryError):
        await learning.learn(run_ids=[run])
    published = memory.read()
    assert REFLECTION in published.blocks["persona.md"]
    monkeypatch.setattr(learning.repository, "save", save)
    learning.repository.recover(learning.clock.now())
    job = learning.repository.latest_job("priority")
    assert learning.reconcile(job["id"])["outcome"] == "completed"
    assert memory.read() == published
    assert len(provider.requests) == 3
