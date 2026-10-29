import json

import pytest

from personal_agent.character.context import CharacterContextCompiler
from personal_agent.character.loader import CharacterLoader
from personal_agent.character.models import CharacterError, CharacterPolicy
from personal_agent.config import Settings
from personal_agent.core.agent import Agent
from personal_agent.core.budget import ContextBudget, estimate_request
from personal_agent.core.context import CHARACTER_HOST_PROMPT, DEFAULT_SYSTEM_PROMPT, ContextBuilder
from personal_agent.core.models import ModelResponse, TextMessage
from personal_agent.memory.store import MemoryFS
from personal_agent.sessions import SessionManager
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.tools.builtin import create_builtin_registry
from tests.fakes import FakeProvider, add_call


def compiler(**policy):
    return CharacterContextCompiler(
        CharacterLoader().load("sherlock_bbc"), CharacterPolicy(**policy)
    )


async def test_character_view_omits_legacy_persona_preserves_disk_and_host_precedence(tmp_path):
    memory = MemoryFS.for_agent(tmp_path / "memory", "heybuddy")
    memory.initialize()
    memory.edit(
        "persona.md",
        "# Persona\n",
        "# Persona\nLEGACY_OTHER_IDENTITY\n",
        expected_hash=memory.read().hashes["persona.md"],
    )
    before = memory.read()
    provider = FakeProvider(ModelResponse(text="先看证据。"))
    context = ContextBuilder(
        memory=memory, budget=ContextBudget(context_window=64000), character=compiler()
    )
    agent = Agent(provider, create_builtin_registry(), context=context)
    assert (await agent.run("这个方法肯定能提升性能。")).status == "completed"
    request = provider.requests[0]
    assert request[0].role == "system" and "Sherlock" in request[0].text
    assert request[1].role == "system" and request[1].text == CHARACTER_HOST_PROMPT
    assert "只使用已注册的 API 工具" in request[1].text
    assert "helpful personal assistant" not in request[1].text
    core = next(
        json.loads(m.text)
        for m in request
        if isinstance(m, TextMessage) and m.text.startswith('{"kind": "agent_core_memory"')
    )
    assert "persona.md" not in core["blocks"] and "human.md" in core["blocks"]
    assert "MEMORY.md" in core["blocks"]
    assert "LEGACY_OTHER_IDENTITY" not in " ".join(getattr(m, "text", "") for m in request)
    assert memory.read() == before
    assert context.diagnostics()["character"]["extra_appraisal_requests"] == 0
    ordinary = ContextBuilder(memory=memory)
    raw = ordinary.for_request(ordinary.build("你好"))
    assert any("LEGACY_OTHER_IDENTITY" in getattr(m, "text", "") for m in raw)


async def test_identity_persists_through_tool_rounds_without_an_appraisal_call():
    provider = FakeProvider(
        ModelResponse(tool_calls=[add_call()]), ModelResponse(text="结果是 5。")
    )
    agent = Agent(
        provider,
        create_builtin_registry(),
        context=ContextBuilder(budget=ContextBudget(context_window=64000), character=compiler()),
    )
    assert (await agent.run("计算后给我结论。 ")).status == "completed"
    assert len(provider.requests) == 2
    assert all("Sherlock" in request[0].text for request in provider.requests)
    assert all(request[0].role == "system" for request in provider.requests)
    assert all(
        estimate_request(request, tools) < 64000
        for request, tools in zip(provider.requests, provider.tools, strict=True)
    )


def test_oversized_static_personality_is_explicit_configuration_error():
    with pytest.raises(CharacterError, match="character_base_budget"):
        compiler(max_context_bytes=1024)


async def test_total_context_budget_accounts_for_character_without_dropping_current_input():
    provider = FakeProvider()
    agent = Agent(
        provider,
        create_builtin_registry(),
        context=ContextBuilder(
            character=compiler(),
            budget=ContextBudget(
                context_window=4096,
                reserved_output_tokens=512,
                summary_tokens=64,
                summary_output_tokens=512,
            ),
        ),
    )
    result = await agent.run("此输入不能被角色设定挤掉。")
    assert result.status == "context_error" and not provider.requests


def test_lore_is_bounded_canonical_reference_not_entire_file():
    character = compiler()
    normal = character.compile("解释一个排序算法")
    assert len(normal) == 1
    contextual = character.compile("John Watson 与贝克街的关系是什么？")
    assert len(contextual) == 2
    data = json.loads(contextual[1].text)
    assert 1 <= len(data["canonical_backstory_not_shared_history"]) <= 2
    assert "current_user_history" not in data
    assert character.diagnostics()["compiled_bytes"] <= 24576


def test_character_survives_session_changes_without_journaling_injected_data(tmp_path):
    with SQLiteStore(tmp_path / "sessions.db") as db:
        db.initialize()
        sessions = SessionManager(db, character=compiler())
        before = sessions.current.id
        first = sessions.context()
        sessions.new()
        assert sessions.context().character is first.character
        sessions.resume(before)
        assert sessions.context().character is first.character
        assert db.load_messages(before) == []


def test_default_settings_leave_character_disabled_and_validate_id():
    settings = Settings(api_key="synthetic", model="synthetic", context_window=64000)
    assert settings.character_id == "none"
    assert settings.character_policy().max_context_bytes == 24576


def test_character_layout_keeps_host_contract_last_and_preserves_custom_host():
    for host in (DEFAULT_SYSTEM_PROMPT, "CUSTOM HOST CONTRACT"):
        context = ContextBuilder(system_prompt=host, character=compiler())
        request = context.for_request(context.build("你好"))
        assert "【说话方式】" in request[0].text
        assert "【情绪反应】" in request[0].text
        assert "【存在方式】" in request[0].text
        assert "character_identity" in request[0].text
        assert request[1].text == (CHARACTER_HOST_PROMPT if host == DEFAULT_SYSTEM_PROMPT else host)
        assert request[-1].text == "你好"
    ordinary = ContextBuilder()
    assert ordinary.for_request(ordinary.build("你好"))[0].text == DEFAULT_SYSTEM_PROMPT


def test_default_character_instructions_are_chinese_without_changing_machine_identity():
    context = ContextBuilder(character=compiler())
    request = context.for_request(context.build("你好"))
    role, host = request[:2]
    assert '"character_id": "sherlock_bbc"' in role.text
    assert "【日常交谈】" in role.text
    assert "用户：你好" in role.text and "【宿主执行契约】" in host.text
    assert "只有 same_character 来源标签证明角色参与" in role.text
    assert "expected_hash" in host.text and "recall_search" in host.text
    for old_label in ("CHARACTER LAYER", "Situation：", "User：", "Context："):
        assert old_label not in role.text
