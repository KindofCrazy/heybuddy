import json

import pytest

from personal_agent.evaluation.character import OfflineProvider, evaluate
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
