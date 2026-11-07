"""Paid evaluation is tested with fake usage only, under the suite's network guard."""

import json
from pathlib import Path

import pytest

from personal_agent.core.models import TextMessage, TokenUsage
from personal_agent.evaluation.heybuddy_bench.analysis import repair_observation, summarize
from personal_agent.evaluation.heybuddy_bench.dataset import load_inputs, read_rows
from personal_agent.evaluation.heybuddy_bench.experiment import SessionBudget, reviewed_score
from personal_agent.evaluation.runner import EvaluationLimit


def protocol():
    return dict(
        max_requests=4, max_tokens=100000, max_cost_cny=1, input_price_cny=2, output_price_cny=8
    )


def test_settlement_and_restart_share_actual_budget(tmp_path):
    path = tmp_path / "ledger.json"
    budget = SessionBudget(path, protocol(), "fixed")
    messages = [TextMessage(role="user", text="fixture")]
    budget.reserve(messages, [], 16384)
    assert budget.state()["pending"]
    budget.observe(TokenUsage(input_tokens=100, output_tokens=50, reasoning_tokens=25))
    assert budget.state()["cost_cny_upper_estimate"] == 0.0006
    assert budget.reserved_tokens == 150
    resumed = SessionBudget(path, protocol(), "fixed")
    assert resumed.requests == 1 and resumed.reserved_cost == 0.0006
    resumed.reserve(messages, [], 16384)
    resumed.observe(TokenUsage(input_tokens=100, output_tokens=50))
    assert resumed.state()["cost_cny_upper_estimate"] == 0.0012


@pytest.mark.parametrize("unknown", [False, True])
def test_pending_or_unknown_usage_never_retries(tmp_path, unknown):
    path = tmp_path / "ledger.json"
    budget = SessionBudget(path, protocol(), "fixed")
    budget.reserve([TextMessage(role="user", text="fixture")], [], 1024)
    if unknown:
        budget.observe(None)
    with pytest.raises(EvaluationLimit, match="requires_review"):
        SessionBudget(path, protocol(), "fixed")
    assert json.loads(path.read_text())["requests"] == 1


def test_price_protocol_change_and_cost_cap_rejected(tmp_path):
    path = tmp_path / "ledger.json"
    budget = SessionBudget(path, protocol(), "fixed")
    with pytest.raises(EvaluationLimit, match="budget_exhausted"):
        budget.reserve([TextMessage(role="user", text="fixture")], [], 200000)
    assert not path.exists()


def test_legacy_observation_repair_uses_only_current_case_calls():
    row = dict(
        track="memory_learning",
        prediction=dict(
            action="update",
            outcome="completed",
            changes=[],
            decisions=[dict(action="update", new_text="previous case")],
        ),
        calls=[dict(schema="Extraction", parsed={"candidates": []})],
    )
    repaired = repair_observation(row)
    assert repaired["prediction"]["action"] == "no_op"
    assert repaired["prediction"]["model_decisions"] == []
    assert row["prediction"]["action"] == "update"  # Original evidence remains unchanged.


def test_live_protocol_is_a_disjoint_complete_group_split():
    root = Path(__file__).resolve().parents[1] / "evals/heybuddy_bench"
    frozen = json.loads((root / "live_protocol.json").read_text(encoding="utf-8"))
    cases = load_inputs(root)
    dev, holdout = set(frozen["development"]), set(frozen["holdout"])
    assert len(dev) == 104 and len(holdout) == 46
    assert not dev.intersection(holdout)
    assert dev.union(holdout) == {case["case_id"] for case in cases}
    for user in {"U03", "U07", "U10"}:
        assert all(case["case_id"] in holdout for case in cases if case.get("user_id") == user)


def test_reviewed_scoring_allows_valid_directories_but_rejects_traversal():
    cases = [dict(case_id="one", track="memory_learning")]
    gold = [dict(case_id="one", gold=dict(action="create", target_path="topics/reference.md"))]
    protocol = dict(review_alternatives={})
    result = dict(
        case_id="one",
        prediction=dict(action="create", changes=[dict(path="projects/notes.md", after="A fact")]),
    )
    assert reviewed_score(cases, gold, [result], protocol)[0]["reviewed_correct"]
    result["prediction"]["changes"][0]["path"] = "projects/../../escape.md"
    assert not reviewed_score(cases, gold, [result], protocol)[0]["reviewed_correct"]


def test_public_summary_keeps_case_cost_and_targets_without_model_text():
    root = Path(__file__).resolve().parents[1] / "evals/heybuddy_bench"
    rows = read_rows(root / "results/baseline_predictions.jsonl")
    for row in rows:
        row["usage"]["cost_cny_upper_estimate"] = 0
    report = summarize(
        load_inputs(root), read_rows(root / "gold.jsonl"), rows, dict(review_alternatives={})
    )
    assert len(report["cases"]) == 150
    for case in report["cases"]:
        assert case["usage"]["cost_cny_upper_estimate"] == 0
        assert "predicted_targets" in case
        assert not {"calls", "prediction", "changes", "decisions"}.intersection(case)
