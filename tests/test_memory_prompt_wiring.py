"""Offline request wiring and no-write boundaries; not a model quality evaluation."""

import pytest

from personal_agent.character.context import CharacterContextCompiler
from personal_agent.character.loader import CharacterLoader
from personal_agent.core.context import ContextBuilder
from personal_agent.core.prompts import MEMORY_READ_RULES
from personal_agent.reflection.prompts import (
    CONSOLIDATION_RULES,
    EXTRACTION_RULES,
    SELECTION_RULES,
)
from tests.test_learning import learner, script
from tests.test_reflection import candidate, decision, scripted, service, state

__all__ = ["state"]


@pytest.mark.parametrize("character_id", [None, "luna", "sherlock_bbc"])
def test_memory_read_policy_reaches_default_and_role_chat_once(character_id):
    character = (
        CharacterContextCompiler(CharacterLoader().load(character_id)) if character_id else None
    )
    builder = ContextBuilder(character=character)
    messages = builder.for_request(builder.build("你好"))
    assert messages[0].text.count(MEMORY_READ_RULES) == 1
    assert all(MEMORY_READ_RULES not in m.text for m in messages[1:])


async def test_reflection_proposal_receives_stage_rules_without_publishing(state):
    db, sessions, memory, run = state
    before = memory.read().blocks
    provider = scripted([candidate(sessions.current.id, run)], [decision()])
    reflection = service(db, memory, provider)
    plan = await reflection.reflect(sessions.current.id)
    assert plan is not None
    assert memory.read().blocks == before
    for request, rules in zip(
        provider.requests,
        (EXTRACTION_RULES, SELECTION_RULES, CONSOLIDATION_RULES),
        strict=True,
    ):
        assert rules in request[0].text
        assert MEMORY_READ_RULES not in request[0].text
    assert all(tools == [] for tools in provider.tools)


async def test_learning_no_signal_stops_after_extraction_without_memory_changes(state):
    _, _, memory, run = state
    before = memory.read().blocks
    provider = script([])
    report = await learner(state, provider).learn(run_ids=[run])
    assert report["outcome"] == "completed"
    assert memory.read().blocks == before
    assert len(provider.requests) == 1
    assert EXTRACTION_RULES in provider.requests[0][0].text
    assert provider.tools[0] == []
