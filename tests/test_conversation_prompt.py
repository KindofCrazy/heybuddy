"""Shared chat style is independent of role cards and does not reach maintenance models."""

import hashlib

import pytest

from personal_agent.character.context import CharacterContextCompiler
from personal_agent.character.loader import CharacterLoader
from personal_agent.core.context import ContextBuilder
from personal_agent.core.prompts import CONVERSATION_PROMPT, REMINDER_RULES
from personal_agent.reflection import service as reflection


@pytest.mark.parametrize("character_id", [None, "luna", "sherlock_bbc"])
def test_shared_style_is_in_single_system_once_for_default_and_character_chat(character_id):
    character = (
        CharacterContextCompiler(CharacterLoader().load(character_id)) if character_id else None
    )
    context = ContextBuilder(character=character)
    messages = context.for_request(context.build("你好"))
    assert sum(m.role == "system" for m in messages) == 1
    assert messages[0].text.count(CONVERSATION_PROMPT) == 1
    assert messages[0].text.count(REMINDER_RULES) == 1
    if character:
        assert (
            hashlib.sha256(messages[0].text.encode()).hexdigest()
            == character.diagnostics()["system_hash"]
        )
    assert all(CONVERSATION_PROMPT not in m.text for m in messages if m.role != "system")


def test_shared_style_does_not_change_reflection_structured_instructions():
    for name in ("BASE", "EXTRACT", "SELECT", "CONSOLIDATE"):
        assert CONVERSATION_PROMPT not in getattr(reflection, name)
        assert REMINDER_RULES not in getattr(reflection, name)
