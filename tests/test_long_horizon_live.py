"""Offline Contract Tests for the explicitly gated live smoke, never real API calls."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from personal_agent.core.models import ModelResponse, TextMessage, TokenUsage
from personal_agent.evaluation.long_horizon.__main__ import main
from personal_agent.evaluation.long_horizon.dataset import digest, load
from personal_agent.evaluation.long_horizon.live import (
    DevLiveConfig,
    LiveConfig,
    LiveStopped,
    MeteredProvider,
    evaluate_live,
    recompute_live,
    validate_live_scope,
)
from personal_agent.llm.openai_provider import OpenAIProvider
from tests.test_provider import make_client, response_body, text_item

FIXTURES = Path(__file__).resolve().parents[1] / "evals/long_horizon"


def config(**overrides):
    return LiveConfig(
        **dict(
            dict(
                model="synthetic",
                endpoint_host="mock.invalid",
                context_window=131072,
                input_price_cny=2.0,
                output_price_cny=8.0,
                price_source="mock-price",
                repetitions=1,
            ),
            **overrides,
        )
    )


class Fixed:
    def __init__(self, *, text='{"action":"clarify"}', usage=True):
        self.requests = []
        self.text, self.usage = text, usage

    async def generate(self, messages, tools, **options):
        self.requests.append(messages)
        assert tools == [] and options == {"max_output_tokens": 1024}
        return ModelResponse(
            text=self.text,
            usage=(
                TokenUsage(input_tokens=300, output_tokens=40, reasoning_tokens=10)
                if self.usage
                else None
            ),
        )


async def test_live_smoke_mock_transport_and_recompute(tmp_path):
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        assert payload["tools"] == [] and payload["max_output_tokens"] == 1024
        assert "gold_topic_id" not in request.content.decode()
        assert "reference_answer" not in request.content.decode()
        requests.append(payload)
        body = response_body([text_item('{"action":"clarify"}')])
        body["usage"] = {
            "input_tokens": 300,
            "output_tokens": 40,
            "total_tokens": 340,
            "output_tokens_details": {"reasoning_tokens": 10},
        }
        return httpx.Response(200, json=body)

    output = tmp_path / "live-mock"
    async with make_client(handle) as client:
        report = await evaluate_live(
            load(FIXTURES), output, OpenAIProvider(client, "synthetic"), config(repetitions=2)
        )
    assert len(requests) == report["model_decision_support"] == 2
    assert report["rule_decision_support"] == 22
    assert report["model_clarifications"] == 2
    assert report["selection_unavailable"] == 0
    assert report["usage"]["input_tokens"] == 600
    assert report["usage"]["output_tokens"] == 80  # Reasoning already included, not added again.
    assert report["usage"]["cost_cny_upper_estimate"] == pytest.approx(0.00184)
    assert len(report["trials"]) == 16
    assert recompute_live(output) == report
    public = json.dumps(report, ensure_ascii=False)
    for s in load(FIXTURES).corpus.scenarios:
        for t in s.turns:
            assert t.user_message not in public
    raw = json.loads((output / "live-trace.json").read_text(encoding="utf-8"))
    raw["calls"][0]["usage"]["input_tokens"] += 1
    (output / "live-trace.json").write_text(json.dumps(raw), encoding="utf-8")
    (output / "trace-hash.json").write_text(json.dumps({"sha256": digest(raw)}))
    with pytest.raises(ValueError, match="usage/budget"):
        recompute_live(output)


@pytest.mark.parametrize("limit", ["requests", "tokens", "money", "input"])
async def test_budget_reserves_before_network(limit):
    changes = {
        "requests": {"max_requests": 1},
        "tokens": {"max_tokens": 1},
        "money": {"max_cost_cny": 0.000001},
        "input": {"max_input_tokens": 1},
    }
    fixed = Fixed()
    metered = MeteredProvider(fixed, config(**changes[limit]))
    request = [TextMessage(role="system", text="route"), TextMessage(role="user", text="data")]
    if limit == "requests":
        await metered.generate(request, [], max_output_tokens=1024)
    with pytest.raises(LiveStopped):
        await metered.generate(request, [], max_output_tokens=1024)
    assert len(fixed.requests) == (1 if limit == "requests" else 0)


async def test_unknown_usage_stops_before_next_paid_request(tmp_path):
    fixed = Fixed(usage=False)
    output = tmp_path / "unknown"
    with pytest.raises(LiveStopped):
        await evaluate_live(load(FIXTURES), output, fixed, config(repetitions=5))
    assert len(fixed.requests) == 1
    status = json.loads((output / "status.json").read_text())
    assert status["status"] == "failed" and status["error_code"] == "usage_unknown"
    assert status["usage"]["input_tokens"] == "unknown"
    metadata = json.loads((output / "live-metadata.json").read_text())
    assert metadata["live_config"]["max_requests"] == 5
    assert metadata["code_hash"] and metadata["hashes"]["gold_hash"]
    assert not (output / "summary.json").exists()
    with pytest.raises(ValueError, match="incomplete"):
        recompute_live(output)


@pytest.mark.parametrize("kind", ["api-error", "incomplete"])
async def test_provider_error_swallowed_by_router_is_still_terminal(tmp_path, kind):
    requests = []

    def handle(request):
        requests.append(request)
        if kind == "api-error":
            return httpx.Response(500, json={"error": {"message": "private synthetic detail"}})
        body = response_body([], status="incomplete")
        body["incomplete_details"] = {"reason": "max_output_tokens"}
        body["usage"] = {"input_tokens": 300, "output_tokens": 1024, "total_tokens": 1324}
        return httpx.Response(200, json=body)

    output = tmp_path / kind
    async with make_client(handle) as client:
        with pytest.raises(LiveStopped):
            await evaluate_live(
                load(FIXTURES), output, OpenAIProvider(client, "synthetic"), config()
            )
    assert len(requests) == 1
    status = json.loads((output / "status.json").read_text())
    assert status["status"] == "failed"
    assert status["error_code"] == ("api_error" if kind == "api-error" else "incomplete_response")
    assert "private synthetic detail" not in json.dumps(status)


async def test_invalid_selection_is_reported_as_decision_failure(tmp_path):
    report = await evaluate_live(
        load(FIXTURES), tmp_path / "invalid", Fixed(text="invalid"), config()
    )
    assert report["selection_unavailable"] == 1
    assert report["model_clarifications"] == 0
    assert report["model_decision_support"] == 1


async def test_cancelled_call_is_not_retried_or_completed(tmp_path):
    class Cancel:
        async def generate(self, *args, **kwargs):
            raise asyncio.CancelledError

    output = tmp_path / "cancel"
    with pytest.raises(asyncio.CancelledError):
        await evaluate_live(load(FIXTURES), output, Cancel(), config())
    status = json.loads((output / "status.json").read_text())
    assert status["status"] == "cancelled" and status["usage"]["actual_requests"] == 1
    assert not (output / "summary.json").exists()


async def test_router_timeout_is_a_failure_not_a_successful_fallback(tmp_path, monkeypatch):
    original = asyncio.timeout
    monkeypatch.setattr(
        asyncio, "timeout", lambda seconds: original(0.001 if seconds == 10 else seconds)
    )

    class Slow:
        async def generate(self, *args, **kwargs):
            await asyncio.sleep(5)

    output = tmp_path / "timeout"
    with pytest.raises(LiveStopped):
        await evaluate_live(load(FIXTURES), output, Slow(), config())
    status = json.loads((output / "status.json").read_text())
    assert status["status"] == "failed" and status["error_code"] == "router_timeout"
    assert status["usage"]["actual_requests"] == 1


async def test_actual_cost_is_checked_after_estimated_reservation(tmp_path):
    class Expensive(Fixed):
        async def generate(self, *args, **kwargs):
            response = await super().generate(*args, **kwargs)
            return response.model_copy(
                update={"usage": TokenUsage(input_tokens=2048, output_tokens=1024)}
            )

    output = tmp_path / "cost-overrun"
    with pytest.raises(LiveStopped):
        await evaluate_live(load(FIXTURES), output, Expensive(), config(max_cost_cny=0.012))
    status = json.loads((output / "status.json").read_text())
    assert status["status"] == "failed" and status["error_code"] == "actual_cost_limit"
    assert status["usage"]["actual_requests"] == 1


@pytest.mark.parametrize(
    "limits",
    [
        {"max_cost_cny": 11.0},
        {"max_requests": 6},
        {"max_tokens": 20001},
        {"input_price_cny": float("nan")},
    ],
)
def test_smoke_cannot_expand_authorized_budget(limits):
    with pytest.raises(ValidationError):
        config(**limits)


def test_cli_live_gate_precedes_loading_settings(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Must not load settings")

    monkeypatch.setattr("personal_agent.config.load_settings", forbidden)
    for arguments in (["--variant", "model-assisted"], ["--variant", "model-assisted", "--live"]):
        monkeypatch.setattr("sys.argv", ["benchmark", *arguments])
        with pytest.raises(SystemExit):
            main()


def dev_config(**overrides):
    return DevLiveConfig(
        **dict(
            dict(
                model="synthetic",
                endpoint_host="mock.invalid",
                context_window=131072,
                input_price_cny=2.0,
                output_price_cny=8.0,
                price_source="mock-price",
            ),
            **overrides,
        )
    )


@pytest.mark.parametrize(
    "limits",
    [
        {"max_requests": 137},
        {"max_tokens": 696321},
        {"max_cost_cny": 3.01},
        {"repetitions": 2},
        {"max_input_tokens": 4097},
    ],
)
def test_dev_profile_cannot_expand_user_authorization(limits):
    with pytest.raises(ValidationError):
        dev_config(**limits)


def test_dev_profile_rejects_holdout_and_wrong_corpus():
    data = load(FIXTURES.parent / "long_horizon_v1")
    validate_live_scope(data, dev_config(), "dev", None)
    for split, scenario in (("holdout", None), ("dev", "LH001")):
        with pytest.raises(ValueError, match="dev split only"):
            validate_live_scope(data, dev_config(), split, scenario)
    with pytest.raises(ValueError, match="dev split only"):
        validate_live_scope(load(FIXTURES), dev_config(), "dev", None)


async def test_one_pass_dev_profile_exceeds_smoke_budget_and_recomputes(tmp_path):
    fixed = Fixed(text='{"action":"continue"}')
    output = tmp_path / "dev-profile"
    report = await evaluate_live(
        load(FIXTURES.parent / "long_horizon_v1"), output, fixed, dev_config()
    )
    assert 5 < len(fixed.requests) <= 136
    assert report["live_config"]["profile"] == "routing-dev-v1"
    assert len(report["trials"]) == 16
    assert {t["scenario_id"] for t in report["trials"]} == {f"LH{n:03d}" for n in range(1, 9)}
    assert report["usage"]["cost_cny_upper_estimate"] < 3
    assert recompute_live(output) == report
    assert report["annotation_status"] == "agent-authored provisional gold"
