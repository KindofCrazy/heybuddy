import json

import pytest

from personal_agent.evaluation.character import OfflineProvider, Trace, evaluate
from personal_agent.evaluation.character_cases import CASES, RUBRIC
from personal_agent.evaluation.runner import Budget, EvaluationLimit


async def test_all_ablations_share_conditions_but_differ_in_character_layers():
    reports = [await evaluate(v, OfflineProvider(), Budget()) for v in ("E0", "E1", "E2")]
    assert len(CASES) >= 12 and len(RUBRIC) == 7
    assert len(next(c for c in CASES if c.id == "drift20").prompts) == 20
    assert all(r["contract_pass"] for r in reports)
    assert all(r["state_update_requests"] == 0 for r in reports)
    assert all(all(axis["score"] is None for axis in r["axes"].values()) for r in reports)
    assert len({r["corpus_hash"] for r in reports}) == 1
    conditions = [[row["condition_hash"] for row in r["results"]] for r in reports]
    assert conditions[0] == conditions[1] == conditions[2]
    for index, report in enumerate(reports):
        first = report["results"][0]["calls"][0]["messages"]
        assert ('"behavior":' in first[1]["text"]) is (index > 0)
        assert any("transient_simulated_state" in m.get("text", "") for m in first) is (index == 2)
        tool = next(r for r in report["results"] if r["case"] == "tool_review")
        assert len(tool["calls"]) == 2
        message = next(m for m in tool["calls"][1]["messages"] if m["role"] == "tool")
        assert message["result"]["call_id"] == "synthetic-add"
        assert message["result"]["ok"] is True
        last = report["results"][-1]
        assert not last["character"]["state_available"]
    assert reports[0]["usage"]["requests"] == reports[1]["usage"]["requests"] == 33
    assert reports[0]["usage"]["reserved_tokens"] < reports[1]["usage"]["reserved_tokens"]


async def test_evaluation_budget_stops_before_additional_provider_call():
    budget = Budget(requests=1)
    with pytest.raises(EvaluationLimit):
        await evaluate("E2", OfflineProvider(), budget)
    assert budget.requests == 1 and budget.exhausted
    with pytest.raises(ValueError):
        await evaluate("E3", OfflineProvider(), Budget())


def test_offline_cli_does_not_load_credentials_or_live_client(tmp_path, monkeypatch, capsys):
    from personal_agent.config import load_settings
    from personal_agent.evaluation import character

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline evaluation must not load settings or keys")

    monkeypatch.setattr("personal_agent.config.load_settings", forbidden)
    assert load_settings is not forbidden
    monkeypatch.setattr(
        "sys.argv",
        ["character", "--output", str(tmp_path), "--cases", "smalltalk", "--variant", "E0"],
    )
    assert character.main() == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["live"] is False and summary["semantic_scores"] == "unreviewed"
    assert summary["usage"]["requests"] == 1


async def test_incomplete_live_response_usage_is_counted_without_saving_body():
    from personal_agent.core.models import ProviderDiagnostics, TextMessage, TokenUsage
    from personal_agent.llm.base import ProviderError
    from tests.fakes import FakeProvider

    budget = Budget()
    provider = Trace(
        FakeProvider(
            ProviderError(
                "PRIVATE_UNFINISHED_BODY",
                code="incomplete_response",
                diagnostics=ProviderDiagnostics(
                    status="incomplete",
                    incomplete_reason="max_output_tokens",
                    usage=TokenUsage(input_tokens=42, output_tokens=512, reasoning_tokens=500),
                ),
            )
        ),
        budget,
    )
    with pytest.raises(ProviderError):
        await provider.generate(
            [TextMessage(role="user", text="synthetic")], [], max_output_tokens=512
        )
    assert budget.report()["output_tokens"] == 512
    assert budget.report()["reasoning_tokens"] == 500
    assert provider.calls[0]["diagnostics"]["incomplete_reason"] == "max_output_tokens"
    assert "PRIVATE_UNFINISHED_BODY" not in json.dumps(provider.calls)


async def test_output_budget_is_part_of_paired_conditions():
    reports = [
        await evaluate(
            "E2", OfflineProvider(), Budget(), case_ids={"smalltalk"}, output_tokens=limit
        )
        for limit in (512, 2048)
    ]
    assert reports[0]["results"][0]["condition_hash"] != reports[1]["results"][0]["condition_hash"]
    assert reports[1]["parameters"]["output_tokens"] == 2048
    assert reports[1]["usage"]["reserved_tokens"] - reports[0]["usage"]["reserved_tokens"] == 1536


def test_real_state_smoke_requires_live_before_loading_configuration(monkeypatch):
    from personal_agent.evaluation import character_state

    def forbidden():
        raise AssertionError("Must not load configuration without --live")

    monkeypatch.setattr(character_state, "load_settings", forbidden)
    monkeypatch.setattr("sys.argv", ["character_state"])
    with pytest.raises(SystemExit) as stopped:
        character_state.main()
    assert stopped.value.code == 2


async def test_trace_omits_reasoning_replay_without_mutating_actual_request():
    from personal_agent.core.models import AssistantMessage, ModelResponse
    from tests.fakes import FakeProvider

    source = AssistantMessage(
        text="visible",
        provider_data={"openai": [{"type": "reasoning", "content": "PRIVATE_REASONING_TEXT"}]},
    )
    inner = FakeProvider(ModelResponse(text="visible answer"))
    trace = Trace(inner, Budget())
    await trace.generate([source], [], max_output_tokens=512)
    assert "PRIVATE_REASONING_TEXT" not in json.dumps(trace.calls)
    assert inner.requests[0][0] == source
    assert source.provider_data["openai"][0]["content"] == "PRIVATE_REASONING_TEXT"
