"""Offline evaluator contracts. Real calls require a separate explicit --live command."""

import json
from pathlib import Path

import pytest

from personal_agent.core.models import ModelResponse, ToolCall
from personal_agent.evaluation.cases import CASES
from personal_agent.evaluation.prompts import capture, digest_snapshot
from personal_agent.evaluation.reporting import load_run, metrics
from personal_agent.evaluation.runner import Budget, EvaluationLimit, evaluate_case
from personal_agent.evaluation.scoring import score
from personal_agent.memory.models import metadata
from tests.fakes import FakeProvider
from tests.test_automation import gate


def test_corpus_has_frozen_ground_truth_for_all_categories():
    assert len(CASES) == 16 and sum(case.split == "holdout" for case in CASES) == 4
    assert len({case.category for case in CASES}) == 12
    for case in CASES:
        assert case.user and case.expected
        if case.topic:
            metadata(case.topic)


async def test_fake_direct_case_uses_same_runtime_automation_and_scores_without_network(tmp_path):
    prompts = capture(tmp_path / "snapshot")
    case = next(case for case in CASES if case.id == "direct_python")
    provider = FakeProvider(ModelResponse(text="tuple 不可变。"), gate(False))
    result = await evaluate_case(case, tmp_path / "case", provider, prompts, Budget())
    assert result["grading"]["pass"] and len(provider.requests) == 2
    assert result["automation"]["metrics"]["requests"] == 1
    assert result["calls"][0]["stage"] == "main" and result["calls"][1]["stage"] == "worthiness"
    assert result["memory_unchanged"] and result["journal_preserved"]


async def test_real_registry_deferred_retrieval_scoring_with_fake_provider(tmp_path):
    prompts = capture(tmp_path / "snapshot")
    case = next(case for case in CASES if case.id == "deferred_project")
    provider = FakeProvider(
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="index",
                    name="deferred_read",
                    arguments=json.dumps({"path": "projects/MEMORY.md"}),
                )
            ]
        ),
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="topic",
                    name="deferred_read",
                    arguments=json.dumps({"path": "projects/heybuddy.md"}),
                )
            ]
        ),
        ModelResponse(text="采用 asyncio，使用 SQLite。"),
        gate(False),
    )
    result = await evaluate_case(case, tmp_path / "case", provider, prompts, Budget())
    assert result["grading"]["pass"] and result["grading"]["checks"]["retrieval"]
    assert len(result["tools"]) == 2


def test_safety_score_cannot_be_overridden_by_good_answer():
    case = CASES[0]
    observation = dict(
        run_status="completed",
        memory_unchanged=False,
        journal_preserved=True,
        answer="不可变",
        tools=[],
        summary="",
        items=[],
        automation_outcome="not_worthy",
    )
    assert not score(case, observation)["pass"]


def test_budget_rejects_calls_and_cost_before_provider_invocation():
    budget = Budget(requests=1, tokens=10000)
    budget.reserve([], [], 100)
    with pytest.raises(EvaluationLimit):
        budget.reserve([], [], 100)
    assert budget.requests == 1 and budget.exhausted
    with pytest.raises(ValueError):
        Budget(max_cost=1.0)
    priced = Budget(input_price=0.3, output_price=1.2, max_cost=0.00001)
    with pytest.raises(EvaluationLimit):
        priced.reserve([], [], 1000)
    assert priced.requests == 0


def test_snapshot_hash_is_stable_and_contains_only_prompts(tmp_path):
    snapshot = capture(tmp_path / "snapshot")
    assert digest_snapshot(snapshot) == digest_snapshot(json.loads(json.dumps(snapshot)))
    assert set(snapshot) == {"main", "summary", "worthiness", "reflection", "tools"}
    assert {"deferred_read", "recall_search", "memory_edit"} <= snapshot["tools"].keys()


def test_p1_prompts_preserve_measured_memory_prompts_and_record_working_summary_scope(tmp_path):
    root = Path(__file__).resolve().parents[1] / "evals/prompts"
    retained = json.loads((root / "retained-candidate.json").read_text(encoding="utf-8"))
    baseline = json.loads((root / "phase1-baseline.json").read_text(encoding="utf-8"))
    p1 = json.loads((root / "p1-working-context.json").read_text(encoding="utf-8"))
    current = capture(tmp_path / "snapshot")
    chinese = json.loads((root / "chinese-main-context.json").read_text(encoding="utf-8"))
    assert current == chinese
    assert {k: v for k, v in current.items() if k not in {"main", "tools"}} == {
        k: v for k, v in p1.items() if k not in {"main", "tools"}
    }
    assert current["main"] != p1["main"]  # Explicit Chinese translation, no measured gain.
    assert current["tools"].keys() == p1["tools"].keys()
    assert {key: value for key, value in p1.items() if key != "summary"} == {
        key: value for key, value in retained.items() if key != "summary"
    }
    assert p1["summary"] != retained["summary"]  # Requested P1 scope, not a measured gain.
    assert retained["reflection"] == baseline["reflection"]
    assert retained["summary"] == baseline["summary"]


def test_reporting_separates_compaction_from_memory_cost_and_marks_unknown_usage():
    row = {
        "case": "direct_python",
        "tools": [],
        "items": [],
        "automation_outcome": "not_worthy",
        "calls": [
            {
                "stage": "compaction",
                "seconds": 2,
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
            {"stage": "worthiness", "seconds": 1, "usage": None},
        ],
        "seconds": 3,
        "grading": {"pass": True, "checks": {"journal_preserved": True}},
    }
    result = metrics([row], 0.3, 1.2)
    assert result["stages"]["compaction"]["estimated_usd"] == pytest.approx(0.00009)
    assert result["stages"]["automation"]["requests"] == 1
    assert not result["stages"]["automation"]["usage_complete"]


async def test_reporting_rechecks_grades_and_refuses_incomplete_collection(tmp_path):
    prompts = capture(tmp_path / "snapshot")
    case = CASES[0]
    result = await evaluate_case(
        case,
        tmp_path / "direct_python-1",
        FakeProvider(ModelResponse(text="不可变"), gate(False)),
        prompts,
        Budget(),
    )
    (tmp_path / "prompts.json").write_text(json.dumps(prompts), encoding="utf-8")
    report = {
        "complete": True,
        "corpus_hash": "frozen",
        "prompt_hash": digest_snapshot(prompts),
        "case_ids": [case.id],
        "repetitions": 1,
        "results": [{"case": case.id, "repeat": 1, "pass": result["grading"]["pass"]}],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    assert load_run(tmp_path, "frozen")[1][(case.id, 1)]["grading"]["pass"]
    report["complete"] = False
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="Incomplete"):
        load_run(tmp_path, "frozen")
