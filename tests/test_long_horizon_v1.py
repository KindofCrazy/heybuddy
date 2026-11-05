"""Offline Contract Tests for draft corpus, explicit evidence relations and budget paths."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from personal_agent.evaluation.long_horizon.dataset import load
from personal_agent.evaluation.long_horizon.metrics import aggregate, score
from personal_agent.evaluation.long_horizon.models import Prediction, RoutingGold, ScenarioGold
from personal_agent.evaluation.long_horizon.preflight import BudgetProbe, preflight
from personal_agent.evaluation.long_horizon.report import evaluate, recompute

DATA = Path(__file__).resolve().parents[1] / "evals/long_horizon_v1"


def test_v1_splits_coverage_chronology_and_annotation_honesty():
    data = load(DATA)
    assert len(data.select("dev")) == 8 and len(data.select("holdout")) == 4
    assert len(data.corpus.scenarios) == 12
    assert data.protocol.human_reviewed is False
    ids = []
    for scenario in data.corpus.scenarios:
        assert len(scenario.turns) == 18
        assert len({t.timestamp.date() for t in scenario.turns}) == 3
        ids.extend(t.turn_id for t in scenario.turns)
        gold = data.gold_for(scenario.scenario_id)
        assert len(gold.routing) == 18
        for index, row in enumerate(gold.routing):
            assert all(ref in ids and ref != row.turn_id for ref in row.relevant_source_turn_ids)
            assert all(
                ref in [t.turn_id for t in scenario.turns[:index]]
                for ref in row.relevant_source_turn_ids
            )
        assert "topic_a" not in json.dumps(scenario.model_dump(mode="json"))
    assert len(ids) == len(set(ids)) == 216
    assert "protocol_hash" in data.hashes


def test_protocol_is_frozen_and_required_when_declared(tmp_path):
    for path in DATA.glob("*.json"):
        (tmp_path / path.name).write_bytes(path.read_bytes())
    raw = json.loads((tmp_path / "protocol.json").read_text(encoding="utf-8"))
    raw["human_reviewed"] = True
    (tmp_path / "protocol.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="protocol hash"):
        load(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    manifest.pop("protocol_hash")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="requires a frozen"):
        load(tmp_path)


def label(turn, topic, refs=(), actions=("continue",)):
    return RoutingGold(
        turn_id=turn,
        gold_topic_id=topic,
        ambiguity=topic is None,
        acceptable_actions=list(actions),
        relevant_source_turn_ids=list(refs),
    )


def prediction(turn, thread, *, clarify=False):
    return Prediction(
        scenario_id="TEST001",
        turn_id=turn,
        actual_thread_id=thread,
        previous_thread_id=None,
        routing_reason="test",
        whether_new_thread_created=False,
        ambiguity_flag=clarify,
        request_count=0,
        latency_ms=0.0,
    )


def test_missed_and_unnecessary_clarification_and_observed_error_streaks():
    gold = ScenarioGold(
        scenario_id="TEST001",
        routing=[
            label("1", "a", actions=("new",)),
            label("2", "b", ("1",), ("new",)),
            label("3", "b", ("2", "1")),
            label("4", None, ("1", "3"), ("clarify",)),
            label("5", "a", ("1", "3"), ("continue", "select")),
        ],
        memory=[],
        checkpoints=[],
    )
    predictions = [prediction(str(i), "merged", clarify=i == 5) for i in range(1, 6)]
    result = score(predictions, gold)
    assert result["clarification"]["miss_rate"] == {"numerator": 1, "support": 1, "rate": 1.0}
    assert result["clarification"]["unnecessary_rate"]["rate"] == 0.25
    assert result["clarification"]["precision"]["rate"] == 0
    assert result["routing_coverage"]["rate"] == 0.75
    assert result["reference_relation_accuracy"]["rate"] == pytest.approx(1 / 3)
    propagation = result["error_propagation"]
    assert propagation["max_consecutive_failed_turns"] == 2
    assert propagation["failure_streaks_ge2"] == 1
    assert propagation["adjacent_failure_rate"]["rate"] == 1
    assert propagation["evaluated_turn_support"] == 2
    assert result["action_accuracy"] is None
    aggregated = aggregate([result])
    assert aggregated["clarification"]["miss_rate"]["micro"]["support"] == 1


def test_optional_clarification_and_no_reference_support_are_not_forced_wrong():
    gold = ScenarioGold(
        scenario_id="TEST001",
        routing=[label("1", None, actions=("continue", "clarify"))],
        memory=[],
        checkpoints=[],
    )
    result = score([prediction("1", "thread")], gold)
    assert result["clarification"]["miss_rate"]["rate"] is None
    assert result["reference_relation_accuracy"]["support"] == 0
    assert aggregate([result])["reference_relation_accuracy"]["macro"]["scenario_support"] == 0
    with pytest.raises(ValidationError):
        label("2", "a", actions=())


async def test_preflight_uses_production_router_without_gold_or_keys(monkeypatch):
    original = BudgetProbe.generate
    seen = []

    async def spy(provider, messages, tools, **options):
        text = json.dumps([m.model_dump(mode="json") for m in messages], ensure_ascii=False)
        assert "gold_topic_id" not in text and "topic_a" not in text
        assert "acceptable_actions" not in text and "human_reviewed" not in text
        seen.append(provider.action)
        return await original(provider, messages, tools, **options)

    monkeypatch.setattr(BudgetProbe, "generate", spy)
    report = await preflight(load(DATA), scenario_id="LH001")
    assert report["actual_network_requests"] == 0
    assert report["single_pass_request_upper_bound"] == 17
    assert report["single_pass_reserved_tokens_at_cap"] == 17 * 5120
    assert set(seen) == {"continue", "clarify", "new"}
    assert "predictions" not in report and "pairwise" not in json.dumps(report)


async def test_protocol_survives_saved_report_and_recompute(tmp_path):
    output = tmp_path / "v1"
    data = load(DATA)
    report = await evaluate(data, output, scenario_id="LH001")
    assert report["annotation_status"] == "agent-authored provisional gold"
    assert report["protocol_hash"] == data.hashes["protocol_hash"]
    assert recompute(output) == report


def test_preflight_cli_never_loads_live_settings(monkeypatch, tmp_path, capsys):
    from personal_agent.evaluation.long_horizon.__main__ import main

    def forbidden(*args, **kwargs):
        raise AssertionError("Budget preflight cannot load credentials")

    monkeypatch.setattr("personal_agent.config.load_settings", forbidden)
    monkeypatch.setattr(
        "sys.argv",
        [
            "benchmark",
            "--dataset",
            str(DATA),
            "--preflight",
            "--scenario",
            "LH001",
            "--output",
            str(tmp_path / "budget"),
        ],
    )
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["actual_network_requests"] == 0
    monkeypatch.setattr("sys.argv", ["benchmark", "--preflight", "--live"])
    with pytest.raises(SystemExit):
        main()
