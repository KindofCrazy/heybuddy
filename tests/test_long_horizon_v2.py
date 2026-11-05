"""Offline contracts only: production maintenance, frozen ablations and 30 synthetic cases."""

import asyncio
import json
from pathlib import Path

import pytest

from personal_agent.core.models import ModelResponse, TextMessage
from personal_agent.evaluation.long_horizon.ablation import evaluate_ablation, recompute_ablation
from personal_agent.evaluation.long_horizon.dataset import load
from personal_agent.evaluation.long_horizon.metrics import score
from personal_agent.evaluation.long_horizon.models import Scenario, ScenarioGold
from personal_agent.evaluation.long_horizon.policies import PolicyProvider
from personal_agent.evaluation.long_horizon.replay import replay
from personal_agent.evaluation.long_horizon.routing_eval import RoutingAdapter
from personal_agent.evaluation.long_horizon.state import (
    ExtractiveContractProvider,
    ProductionMaintenance,
)
from personal_agent.organization.router import ThreadRouter
from tests.test_long_horizon_v1 import label, prediction

ROOT = Path(__file__).resolve().parents[1] / "evals"


def test_thirty_cases_preserve_v1_and_holdout_partition():
    old = load(ROOT / "long_horizon_v1")
    data = load(ROOT / "long_horizon_v2")
    assert len(data.corpus.scenarios) == 30
    assert sum(len(s.turns) for s in data.corpus.scenarios) == 540
    assert len(data.select("dev")) == 21 and len(data.select("holdout")) == 9
    assert data.corpus.scenarios[:12] == old.corpus.scenarios
    assert data.gold.scenarios[:12] == old.gold.scenarios
    assert set(old.splits.splits["holdout"]) <= set(data.splits.splits["holdout"])
    assert not data.protocol.human_reviewed


async def test_real_state_publication_is_prefix_only_and_follows_wrong_routes():
    scenario = load(ROOT / "long_horizon_v1").corpus.scenarios[0]
    first_future = scenario.turns[3].user_message
    seen = []

    class Inspect(RoutingAdapter):
        async def route(self, repo, run_id, turn, sid):
            candidates = repo.candidates()
            if turn.turn_id.endswith("T04"):
                assert candidates and candidates[0].current_state
                assert not candidates[0].dirty and candidates[0].revision > 1
                assert scenario.turns[0].user_message in candidates[0].current_state
                assert first_future not in candidates[0].current_state
                assert scenario.turns[0].frozen_assistant_message not in candidates[0].current_state
            if turn.turn_id.endswith("T07"):
                # Baseline has incorrectly merged A/B: maintenance must not silently fix ownership.
                assert len(candidates) == 1 and first_future in candidates[0].current_state
                seen.append(True)
            return await super().route(repo, run_id, turn, sid)

    class InspectMaintenance(ProductionMaintenance):
        async def after_completed(self, repo, turn, count):
            with repo.store.reading() as db:
                assert not db.execute("SELECT 1 FROM runs WHERE status='running'").fetchone()
                before = dict(db.execute("SELECT run_id,thread_id FROM run_threads"))
            result = await super().after_completed(repo, turn, count)
            with repo.store.reading() as db:
                assert dict(db.execute("SELECT run_id,thread_id FROM run_threads")) == before
                assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            return result

    maintenance = InspectMaintenance(ExtractiveContractProvider())
    result = await replay(scenario, adapter=Inspect(), maintenance=maintenance)
    assert result.status == "completed" and seen
    assert len(result.maintenance) == 6
    assert {c["stage"] for c in maintenance.calls} == {"episode", "thread"}
    assert all(c["status"] == "completed" for c in maintenance.calls)


async def test_maintenance_failure_and_cancellation_never_become_success():
    scenario = load(ROOT / "long_horizon_v1").corpus.scenarios[0]

    class Bad:
        async def generate(self, *args, **kwargs):
            return ModelResponse(text="invalid")

    failed = await replay(scenario, maintenance=ProductionMaintenance(Bad()))
    assert failed.status == "failed" and failed.error_stage == "maintenance"
    assert len(failed.predictions) == 3

    class Cancel:
        async def generate(self, *args, **kwargs):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await replay(scenario, maintenance=ProductionMaintenance(Cancel()))


async def test_date_and_budget_corrections_reach_the_actual_bounded_state_card():
    raw = load(ROOT / "long_horizon_v1").corpus.scenarios[0].model_dump(mode="json")
    raw["turns"] = raw["turns"][:4]
    texts = [
        "蓝色工作室搬迁方案，本月二十日执行。",
        "搬迁日期调整为本月二十二日，原定二十日作废。",
        "费用上限改为三千元，之前两千元的上限作废。",
        "搬迁通行证随后由小周申请。",
    ]
    for turn, text in zip(raw["turns"], texts, strict=True):
        turn["user_message"] = text
    seen = []

    class Inspect:
        async def generate(self, messages, tools, **options):
            data = json.loads(messages[1].text)
            if data["input"] == texts[3]:
                card = data["threads"][0]["state"]
                assert len(card) <= 400
                assert texts[1] in card and texts[2] in card
                assert texts[3] not in card
                seen.append(card)
            return ModelResponse(text='{"action":"continue"}')

    result = await replay(
        Scenario.model_validate_json(json.dumps(raw)),
        adapter=RoutingAdapter(provider=Inspect(), model_selection=True),
        maintenance=ProductionMaintenance(ExtractiveContractProvider()),
    )
    assert result.status == "completed" and len(seen) == 1


async def test_weak_rule_deferral_is_opt_in_and_reaches_real_router():
    raw = load(ROOT / "long_horizon_v1").corpus.scenarios[0].model_dump(mode="json")
    raw["turns"] = raw["turns"][:3]
    for t, text in zip(
        raw["turns"],
        [
            "橙色项目要整理章节结构，先列大纲",
            "换个话题，紫色项目要改报名流程",
            "那三份目录我补好了。",
        ],
        strict=True,
    ):
        t["user_message"] = text
    scenario = Scenario.model_validate_json(json.dumps(raw))

    class Choices:
        async def generate(self, messages, tools, **options):
            data = json.loads(messages[1].text)
            if "紫色" in data["input"]:
                return ModelResponse(text='{"action":"new"}')
            target = next(t["id"] for t in data["threads"] if "橙色" in t["title"])
            return ModelResponse(text=json.dumps({"action": "select", "thread_id": target}))

    default = await replay(
        scenario, adapter=RoutingAdapter(provider=Choices(), model_selection=True)
    )
    deferred = await replay(
        scenario,
        adapter=RoutingAdapter(provider=Choices(), model_selection=True, defer_weak_rules=True),
    )
    assert default.status == deferred.status == "completed"
    assert default.predictions[2].actual_thread_id == default.predictions[1].actual_thread_id
    assert deferred.predictions[2].actual_thread_id == deferred.predictions[0].actual_thread_id
    assert default.predictions[2].request_count == 0 and deferred.predictions[2].request_count == 1
    with pytest.raises(ValueError):
        ThreadRouter(None, defer_weak_rules=True)


async def test_fewshot_is_independent_frozen_and_does_not_mutate_input():
    supplied = [
        TextMessage(role="system", text="Original routing schema"),
        TextMessage(role="user", text="current input"),
    ]

    class Capture:
        def __init__(self):
            self.requests = []

        async def generate(self, messages, tools, **options):
            self.requests.append(messages)
            assert tools == []
            return ModelResponse(text='{"action":"continue"}')

    target = Capture()
    for policy in ("baseline", "identity", "fewshot"):
        await PolicyProvider(target, policy).generate(supplied, [])
    sizes = [len(m[0].text) for m in target.requests]
    assert sizes[0] < sizes[1] < sizes[2]
    assert supplied[0].text == "Original routing schema"
    assert all(request[1] == supplied[1] for request in target.requests)
    assert (
        "LH001" not in target.requests[-1][0].text and "topic_a" not in target.requests[-1][0].text
    )


def test_fragmentation_and_contamination_are_different():
    gold = ScenarioGold(
        scenario_id="TEST001",
        routing=[label("1", "a"), label("2", "b"), label("3", "a")],
        memory=[],
        checkpoints=[],
    )
    split = score([prediction("1", "x"), prediction("2", "y"), prediction("3", "z")], gold)
    merged = score([prediction(str(n), "x") for n in range(1, 4)], gold)
    assert split["fragmentation"]["mean_extra_assignments"]["rate"] == 0.5
    assert split["cross_thread_contamination"]["mean_extra_assignments"]["rate"] == 0
    assert merged["fragmentation"]["mean_extra_assignments"]["rate"] == 0
    assert merged["cross_thread_contamination"]["mean_extra_assignments"]["rate"] == 1
    abstain = score([prediction(str(n), "x", clarify=True) for n in range(1, 4)], gold)
    assert abstain["fragmentation"]["mean_extra_assignments"]["rate"] is None


async def test_all_ablations_are_contract_only_and_rescore_without_network(tmp_path):
    data = load(ROOT / "long_horizon_v2")
    output = tmp_path / "ablation"
    result = await evaluate_ablation(data, output, scenario_id="LH001")
    assert recompute_ablation(output) == result
    assert result["actual_network_requests"] == 0
    assert result["arms"]["baseline"]["cards_with_state"] == 0
    assert result["arms"]["state"]["cards_with_state"] > 0
    assert (
        result["arms"]["deferred"]["stages"]["routing"]["synthetic_calls"]
        >= result["arms"]["fewshot"]["stages"]["routing"]["synthetic_calls"]
    )
    assert all(arm["model_quality_metrics"] is None for arm in result["arms"].values())
    text = json.dumps(result, ensure_ascii=False)
    assert data.corpus.scenarios[0].turns[0].user_message not in text
    with pytest.raises(ValueError, match="Holdout"):
        await evaluate_ablation(data, tmp_path / "holdout", split="holdout")


def test_ablation_cli_is_offline_and_never_loads_settings(monkeypatch, tmp_path, capsys):
    from personal_agent.evaluation.long_horizon.__main__ import main

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline ablation cannot load model credentials")

    monkeypatch.setattr("personal_agent.config.load_settings", forbidden)
    monkeypatch.setattr(
        "sys.argv",
        [
            "benchmark",
            "--dataset",
            str(ROOT / "long_horizon_v2"),
            "--ablation",
            "baseline",
            "--scenario",
            "LH001",
            "--output",
            str(tmp_path / "cli"),
        ],
    )
    assert main() == 0 and json.loads(capsys.readouterr().out)["actual_network_requests"] == 0
    monkeypatch.setattr("sys.argv", ["benchmark", "--ablation", "all", "--live"])
    with pytest.raises(SystemExit):
        main()
