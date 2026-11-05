"""Offline tests for a shared-budget, real-maintenance v2 live entry."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from personal_agent.core.models import ModelResponse, TextMessage, TokenUsage
from personal_agent.evaluation.long_horizon.__main__ import main
from personal_agent.evaluation.long_horizon.dataset import digest, load
from personal_agent.evaluation.long_horizon.live import LiveStopped, MeteredProvider
from personal_agent.evaluation.long_horizon.pilot import (
    ARMS,
    PilotConfig,
    evaluate_pilot,
    protocol,
    recompute_pilot,
    validate_scope,
)
from personal_agent.evaluation.long_horizon.state import ExtractiveContractProvider

ROOT = Path(__file__).resolve().parents[1]


def config(**overrides):
    return PilotConfig(
        **{
            "model": "deepseek-flash",
            "endpoint_host": "api.deepseek.com",
            "context_window": 131072,
            "input_price_cny": 2.0,
            "output_price_cny": 8.0,
            "price_source": "mock-price",
            **overrides,
        }
    )


class Mixed:
    def __init__(self):
        self.requests = []

    async def generate(self, messages, tools, **options):
        assert not tools and options["reasoning_effort"] == "none"
        data = json.loads(messages[1].text)
        assert "gold_topic_id" not in messages[1].text
        self.requests.append(data)
        if "input" in data:
            assert options["max_output_tokens"] == 1024
            response = ModelResponse(text='{"action":"continue"}')
        else:
            assert options["max_output_tokens"] == 4096
            response = await ExtractiveContractProvider().generate(messages, [])
        return response.model_copy(
            update={"usage": TokenUsage(input_tokens=120, output_tokens=80, reasoning_tokens=0)}
        )


async def test_pilot_real_maintenance_all_arms_shared_accounting_and_recompute(tmp_path):
    provider = Mixed()
    output = tmp_path / "pilot"
    report = await evaluate_pilot(load(ROOT / "evals/long_horizon_v2"), output, provider, config())
    assert recompute_pilot(output) == report
    assert set(report["arms"]) == set(ARMS)
    assert all(a["completed_turns"] == 54 for a in report["arms"].values())
    assert report["usage"]["actual_requests"] == len(provider.requests)
    assert sum(
        u["actual_requests"] for a in report["arms"].values() for u in a["usage_by_stage"].values()
    ) == len(provider.requests)
    assert report["usage"]["reserved_cost_cny"] <= 10
    assert report["usage"]["reasoning_tokens"] == 0
    assert report["arms"]["baseline"]["usage_by_stage"]["episode"]["actual_requests"] == 0
    assert report["arms"]["state"]["usage_by_stage"]["episode"]["actual_requests"] > 0
    assert report["holdout_evaluated"] is False
    public = json.dumps(report, ensure_ascii=False)
    assert "current_state" not in public and "user_message" not in public
    assert all(a["state_semantic_accuracy"] is None for a in report["arms"].values())
    raw = json.loads((output / "pilot-trace.json").read_text(encoding="utf-8"))
    raw["calls"][0]["usage"]["input_tokens"] += 1
    (output / "pilot-trace.json").write_text(json.dumps(raw), encoding="utf-8")
    (output / "trace-hash.json").write_text(json.dumps({"sha256": digest(raw)}), encoding="utf-8")
    with pytest.raises(ValueError, match="usage mismatch"):
        recompute_pilot(output)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_cost_cny", 10.01),
        ("max_requests", 401),
        ("model", "other"),
        ("endpoint_host", "other.invalid"),
        ("reasoning_effort", "high"),
    ],
)
def test_pilot_caps_and_verified_configuration(field, value):
    with pytest.raises(ValidationError):
        config(**{field: value})


def test_pilot_scope_and_frozen_review():
    data = load(ROOT / "evals/long_horizon_v2")
    plan = protocol()
    assert [s.scenario_id for s in validate_scope(data, plan)] == ["LH019", "LH021", "LH022"]
    assert len(plan["state_review"]) == 9
    with pytest.raises(ValueError):
        validate_scope(data, plan, "holdout")
    with pytest.raises(ValueError):
        validate_scope(load(ROOT / "evals/long_horizon_v1"), plan)
    with pytest.raises(ValueError):
        validate_scope(data, plan, scenario_id="LH019")


async def test_root_budget_shared_across_routing_and_maintenance_stops_before_dispatch():
    fake = Mixed()
    root = MeteredProvider(fake, config(max_requests=1))
    request = [
        TextMessage(role="system", text="route"),
        TextMessage(role="user", text='{"input":"hello"}'),
    ]
    root.scope = {"stage": "routing"}
    await root.generate(request, [], max_output_tokens=1024)
    root.scope = {"stage": "episode"}
    with pytest.raises(LiveStopped, match="budget_exhausted"):
        await root.generate(request, [], max_output_tokens=4096)
    assert len(fake.requests) == 1


async def test_unknown_maintenance_usage_cannot_publish_or_continue(tmp_path):
    class Unknown(Mixed):
        async def generate(self, messages, tools, **options):
            response = await super().generate(messages, tools, **options)
            if "runs" in self.requests[-1]:
                response = response.model_copy(update={"usage": None})
            return response

    fake = Unknown()
    output = tmp_path / "unknown"
    with pytest.raises(LiveStopped):
        await evaluate_pilot(load(ROOT / "evals/long_horizon_v2"), output, fake, config())
    status = json.loads((output / "status.json").read_text(encoding="utf-8"))
    assert status["error_code"] == "usage_unknown" and status["status"] == "failed"
    progress = json.loads((output / "pilot-progress.json").read_text(encoding="utf-8"))
    assert progress["calls"][-1]["stage"] == "episode"
    assert progress["trials"][-1]["result"]["maintenance"] == []
    assert progress["trials"][-1]["result"]["error_stage"] == "maintenance"
    assert not (output / "summary.json").exists()
    with pytest.raises(ValueError, match="incomplete"):
        recompute_pilot(output)


def test_pilot_preflight_gate_never_loads_settings(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No settings during offline gate validation")

    monkeypatch.setattr("personal_agent.config.load_settings", forbidden)
    for options in (
        ["--live"],
        ["--scenario", "LH019"],
        ["--split", "holdout"],
        ["--ablation", "all"],
    ):
        monkeypatch.setattr("sys.argv", ["benchmark", "--pilot-preflight", *options])
        with pytest.raises(SystemExit):
            main()
