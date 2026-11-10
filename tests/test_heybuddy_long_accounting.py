"""An explicitly approved unknown request remains a separate permanent cost reservation."""

import importlib.util
import json
from pathlib import Path

import pytest

from personal_agent.core.models import TextMessage, TokenUsage
from personal_agent.evaluation.runner import EvaluationLimit

SCRIPT = Path(__file__).resolve().parents[1] / "evals/heybuddy_bench/v1_0/reconcile.py"


def module():
    spec = importlib.util.spec_from_file_location("bench_reconciliation", SCRIPT)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def pending(tmp_path):
    protocol = dict(
        max_requests=4500,
        max_tokens=9000000,
        input_price_cny=2,
        output_price_cny=8,
        max_cost_cny=20,
    )
    state = dict(
        protocol_hash="test",
        currency="CNY",
        requests=253,
        input_tokens=415396,
        output_tokens=23331,
        reasoning_tokens=0,
        pending=True,
        usage_complete=False,
        reserved_tokens=453854,
        reserved_cost_cny=1.07227,
        cost_cny_upper_estimate=1.01744,
    )
    (tmp_path / "long-session.json").write_text(json.dumps(state), encoding="utf-8")
    directory = tmp_path / "live-long-dev"
    directory.mkdir()
    (directory / "request-0113.json").write_text('{"synthetic":true}', encoding="utf-8")
    proposal = dict(
        ledger_before=state,
        input_ceiling=11031,
        output_ceiling=4096,
        unknown_cost_ceiling=0.05483,
        request_file="request-0113.json",
    )
    (tmp_path / "pending-accounting-proposal.json").write_text(
        json.dumps(proposal), encoding="utf-8"
    )
    return protocol


def test_approved_reservation_preserves_known_usage_and_never_refunds(tmp_path):
    protocol = pending(tmp_path)
    impl = module()
    budget, receipt = impl.settle(tmp_path, protocol, "test")
    assert receipt["reservation"]["retry"] is False
    state = budget.state()
    assert state["input_tokens"] == 415396 and state["output_tokens"] == 23331
    assert not state["usage_complete"] and state["accounting_complete"]
    assert state["cost_cny_upper_estimate"] == pytest.approx(1.07227)
    budget.reserve([TextMessage(role="user", text="New unrelated scenario")], [], 100)
    budget.observe(TokenUsage(input_tokens=10, output_tokens=10))
    restored, _ = impl.settle(tmp_path, protocol, "test")
    assert restored.state()["cost_cny_upper_estimate"] == pytest.approx(1.07237)
    assert restored.state()["unknown_usage_reservations"][0]["cost_cny_ceiling"] == 0.05483
    assert restored.input_tokens == 415406  # No invented token usage for the old request.


def test_new_unknown_usage_freezes_again_instead_of_automatically_settling(tmp_path):
    protocol = pending(tmp_path)
    impl = module()
    budget, _ = impl.settle(tmp_path, protocol, "test")
    budget.reserve([TextMessage(role="user", text="Another scenario")], [], 100)
    budget.observe(None)
    assert not budget.usage_complete
    assert budget.state()["pending"] and not budget.state()["accounting_complete"]
    with pytest.raises(EvaluationLimit):
        impl.settle(tmp_path, protocol, "test")


def test_reservation_cannot_raise_the_total_cost_cap(tmp_path):
    protocol = pending(tmp_path)
    impl = module()
    budget, _ = impl.settle(tmp_path, protocol, "test")
    budget.max_cost = 1.0723
    with pytest.raises(EvaluationLimit):
        budget.reserve([TextMessage(role="user", text="No extra allowance")], [], 4096)
    assert budget.exhausted and not budget.pending
