import json
from types import SimpleNamespace

import pytest

from personal_agent.character.context import CharacterContextCompiler
from personal_agent.character.loader import CharacterLoader
from personal_agent.config import Settings
from personal_agent.core.agent import Agent
from personal_agent.core.budget import ContextBudget
from personal_agent.core.context import ContextBuilder
from personal_agent.core.images import ImageRef
from personal_agent.core.models import ModelResponse, ProviderDiagnostics, TokenUsage
from personal_agent.core.prompts import VISUAL_RULES
from personal_agent.evaluation import quality_runtime
from personal_agent.evaluation.episode_quality_cases import quality_cases
from personal_agent.evaluation.flow_cases import turns, validate_contract
from personal_agent.llm.base import ProviderError
from personal_agent.organization.episodes import EpisodeOrganizer
from personal_agent.organization.models import OrganizationError, OrganizationPolicy, ThreadRepair
from personal_agent.organization.provider import OrganizerModel
from personal_agent.tools.registry import ToolRegistry
from tests.fakes import FakeProvider
from tests.test_episodes import seed, state

__all__ = ["state"]


def test_optional_effort_is_host_configured_without_model_guessing():
    base = dict(api_key="test-key", model="synthetic", context_window=131072)
    assert Settings(**base).organization_policy().episode_reasoning_effort is None
    configured = Settings(**base, organization_episode_reasoning_effort="low")
    assert configured.organization_policy().episode_reasoning_effort == "low"


def test_projected_tool_calls_and_results_reconstruct_real_evidence():
    case = quality_cases("quality-dev")["tool_success"]
    record = case["input"]["runs"][0]
    turn = turns([record])[("synthetic-chat", record["run_id"])]
    assert len(turn.messages) == 4
    assert turn.messages[1].tool_calls[0].id == turn.messages[2].result.call_id
    result = {
        "episodes": [
            {
                "run_ids": [record["run_id"]],
                "title": "计算",
                "summary": "结果为五",
                "highlights": [],
            }
        ]
    }
    validate_contract("episode", result, case)


def test_required_boundaries_use_the_same_production_partition_check():
    case = quality_cases("quality-dev")["source_blame"]
    ids = [r["run_id"] for r in case["input"]["runs"]]
    case["input"]["boundaries"] = [{"before_run_id": ids[1], "strength": "required"}]
    result = {
        "episodes": [{"run_ids": ids, "title": "排练", "summary": "计划排练", "highlights": []}]
    }
    with pytest.raises(OrganizationError, match="required_boundary"):
        validate_contract("episode", result, case)


async def test_episode_effort_is_forwarded_but_unrelated_requests_keep_their_default(state):
    store, stream, repository = state
    rid = seed(store, stream)
    response = dict(episodes=[dict(run_ids=[rid], title="项目", summary="讨论项目", highlights=[])])
    provider = FakeProvider(
        ModelResponse(text=json.dumps(response)),
        ModelResponse(text='{"current_state":"继续项目","status":"active"}'),
    )
    model = OrganizerModel(
        provider,
        ContextBudget(context_window=131072),
        OrganizationPolicy(episode_reasoning_effort="low"),
    )
    assert (await EpisodeOrganizer(repository, model).organize(stream))["outcome"] == "completed"
    await model.ask(ThreadRepair, "rebuild", {})
    assert provider.reasoning_efforts == ["low", None]
    assert provider.tools == [[], []]


async def test_failed_output_usage_is_retained_without_private_exception_body():
    provider = FakeProvider(
        ProviderError(
            "private response body",
            code="incomplete_response",
            diagnostics=ProviderDiagnostics(
                status="incomplete",
                incomplete_reason="max_output_tokens",
                usage=TokenUsage(input_tokens=100, output_tokens=8192, reasoning_tokens=8192),
            ),
        )
    )
    model = OrganizerModel(provider, ContextBudget(context_window=131072), OrganizationPolicy())
    with pytest.raises(OrganizationError, match="provider_failed"):
        await model.ask(ThreadRepair, "rebuild", {})
    assert model.metrics["failure_reason"] == "incomplete_response"
    assert model.metrics["incomplete_reason"] == "max_output_tokens"
    assert model.metrics["usage"]["reasoning_tokens"] == 8192
    assert "private" not in json.dumps(model.metrics)
    assert len(provider.requests) == 1


@pytest.mark.parametrize("character_id", [None, "luna"])
async def test_visual_rules_are_budgeted_in_single_system_but_never_journaled(character_id):
    provider = FakeProvider(ModelResponse(text="四边形"), ModelResponse(text="红色"))
    character = (
        CharacterContextCompiler(CharacterLoader().load(character_id)) if character_id else None
    )
    context = ContextBuilder(budget=ContextBudget(context_window=131072), character=character)
    agent = Agent(provider, ToolRegistry(), context)
    reference = ImageRef(sha256="0" * 64, size_bytes=1, width=1, height=1)
    await agent.run("图里是什么？", images=[reference])
    await agent.run("刚才那个图是什么颜色？")
    for request in provider.requests:
        assert sum(message.role == "system" for message in request) == 1
        assert request[0].text.count(VISUAL_RULES) == 1
    assert all(VISUAL_RULES not in getattr(message, "text", "") for message in context.history)


async def test_quality_runtime_offline_entry_never_loads_credentials(monkeypatch):
    def forbidden():
        raise AssertionError("No configuration access without --live")

    monkeypatch.setattr(quality_runtime, "load_settings", forbidden)
    with pytest.raises(ValueError, match="Explicit --live"):
        await quality_runtime.run(SimpleNamespace(live=False))
