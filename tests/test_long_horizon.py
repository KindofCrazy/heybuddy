"""Offline Contract Tests only; no claims about language-model capability."""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from personal_agent.evaluation.long_horizon.__main__ import main
from personal_agent.evaluation.long_horizon.dataset import digest, load
from personal_agent.evaluation.long_horizon.metrics import aggregate, score
from personal_agent.evaluation.long_horizon.models import Config, Scenario, ScenarioGold
from personal_agent.evaluation.long_horizon.replay import replay
from personal_agent.evaluation.long_horizon.report import evaluate, recompute
from personal_agent.evaluation.long_horizon.routing_eval import NetworkForbidden, RoutingAdapter
from personal_agent.organization.router import ThreadRouter
from personal_agent.storage.sqlite import SQLiteStore

FIXTURES = Path(__file__).resolve().parents[1] / "evals/long_horizon"


@pytest.fixture
def dataset():
    return load(FIXTURES)


@pytest.mark.parametrize("mutation", ["extra", "duplicate", "time", "naive", "checkpoint", "zone"])
def test_strict_schema(dataset, mutation):
    raw = dataset.corpus.scenarios[0].model_dump(mode="json")
    if mutation == "extra":
        raw["gold_topic_id"] = "forbidden"
    elif mutation == "duplicate":
        raw["turns"][1]["turn_id"] = raw["turns"][0]["turn_id"]
    elif mutation == "time":
        raw["turns"][1]["timestamp"] = "2025-01-01T00:00:00+08:00"
    elif mutation == "naive":
        raw["turns"][0]["timestamp"] = "2026-01-01T00:00:00"
    elif mutation == "checkpoint":
        raw["checkpoints"] = [{"checkpoint_id": "x", "after_turn_id": "missing"}]
    else:
        raw["time_zone"] = "invalid/zone"
    with pytest.raises(ValueError):
        Scenario.model_validate_json(json.dumps(raw))


def test_gold_references_intervals_and_frozen_hash(dataset, tmp_path):
    scenario = dataset.corpus.scenarios[-1]
    raw = dataset.gold_for(scenario.scenario_id).model_dump(mode="json")
    raw["memory"][0]["evidence_turn_ids"] = ["missing"]
    with pytest.raises(ValueError, match="evidence"):
        ScenarioGold.model_validate_json(json.dumps(raw)).validate_for(scenario)
    raw = dataset.gold_for(scenario.scenario_id).model_dump(mode="json")
    raw["memory"][0]["valid_until"] = raw["memory"][0]["valid_from"]
    with pytest.raises(ValidationError):
        ScenarioGold.model_validate_json(json.dumps(raw))
    for path in FIXTURES.iterdir():
        (tmp_path / path.name).write_bytes(path.read_bytes())
    (tmp_path / "corpus.json").write_text('{"version":"2","scenarios":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        load(tmp_path)


async def test_real_router_lifecycle_gold_isolation_and_journal(dataset, monkeypatch):
    original = ThreadRouter.route
    seen = []

    async def spy(router, run_id, text, provider):
        assert not router.model_selection
        with router.repository.store.reading() as db:
            row = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            assert row[0] == "running"
            assert (
                db.execute("SELECT COUNT(*) FROM messages WHERE run_id=?", (run_id,)).fetchone()[0]
                == 1
            )
        assert "gold_topic_id" not in text and "reference_answer" not in text
        seen.append(text)
        return await original(router, run_id, text, provider)

    monkeypatch.setattr(ThreadRouter, "route", spy)
    completed = []
    finish = SQLiteStore.finish_run

    def check_finish(store, run_id, result):
        finish(store, run_id, result)
        with store.reading() as db:
            assert db.execute(
                "SELECT thread_id FROM run_threads WHERE run_id=?", (run_id,)
            ).fetchone()
            assert (
                db.execute("SELECT COUNT(*) FROM messages WHERE run_id=?", (run_id,)).fetchone()[0]
                == 2
            )
        completed.append(run_id)

    monkeypatch.setattr(SQLiteStore, "finish_run", check_finish)
    scenario = dataset.corpus.scenarios[0]
    first, second = await replay(scenario), await replay(scenario)
    assert seen == [t.user_message for t in scenario.turns] * 2
    assert len(completed) == 6
    for result in (first, second):
        result.validate_for(scenario)
        p = result.predictions
        assert p[0].actual_thread_id == p[2].actual_thread_id != p[1].actual_thread_id
        assert score(p, dataset.gold_for(scenario.scenario_id))["pairwise"]["f1"] == 1
    assert first.predictions[0].actual_thread_id != second.predictions[0].actual_thread_id


async def test_isolation_memory_checkpoints_and_clock(dataset):
    roots = []

    class Inspect(RoutingAdapter):
        async def route(self, repository, run_id, turn, scenario_id):
            roots.append(repository.store.path)
            with repository.store.reading() as db:
                assert (
                    db.execute("SELECT started_at FROM runs WHERE id=?", (run_id,)).fetchone()[0]
                    == turn.timestamp.isoformat()
                )
            return await super().route(repository, run_id, turn, scenario_id)

    memory_case = dataset.corpus.scenarios[-1]
    a = await replay(memory_case, adapter=Inspect())
    b = await replay(memory_case, adapter=Inspect())
    assert roots[0] != roots[-1] and all(not p.exists() for p in roots)
    assert a.snapshots[0].memory_blocks == b.snapshots[0].memory_blocks
    assert "16点" not in json.dumps(a.snapshots[0].memory_blocks, ensure_ascii=False)
    assert a.snapshots[0].completed_turn_ids == [t.turn_id for t in memory_case.turns]


async def test_ambiguity_and_zero_denominators(dataset):
    case = dataset.corpus.scenarios[3]
    result = await replay(case)
    scored = score(result.predictions, dataset.gold_for(case.scenario_id))
    assert result.predictions[-1].ambiguity_flag
    assert scored["clarification_rate"] == 1 and scored["eligible_turns"] == 2
    assert scored["pairwise"]["pair_support"] == 1
    assert scored["pairwise"]["precision"] is None
    assert aggregate([scored])["macro"]["precision"]["scenario_support"] == 0


async def test_pairwise_errors_are_scores_not_execution_failures(dataset):
    case = dataset.corpus.scenarios[0]
    result = await replay(case)
    wrong = [p.model_copy(update={"actual_thread_id": "one-thread"}) for p in result.predictions]
    scored = score(wrong, dataset.gold_for(case.scenario_id))["pairwise"]
    assert scored["tp"] == 1 and scored["fp"] == 2 and scored["f1"] == 0.5


async def test_exception_and_cancellation_are_not_success(dataset):
    class Broken(RoutingAdapter):
        async def route(self, *args):
            raise RuntimeError("synthetic failure")

    class Cancel(RoutingAdapter):
        async def route(self, *args):
            raise asyncio.CancelledError

    case = dataset.corpus.scenarios[0]
    failed = await replay(case, adapter=Broken())
    assert failed.status == "failed" and failed.error_stage == "routing"
    with pytest.raises(ValueError, match="incomplete"):
        failed.validate_for(case)
    with pytest.raises(asyncio.CancelledError):
        await replay(case, adapter=Cancel())
    with pytest.raises(RuntimeError):
        await NetworkForbidden().generate([], [])


async def test_report_recompute_privacy_and_incomplete(dataset, tmp_path):
    output = tmp_path / "results"
    report = await evaluate(dataset, output)
    assert recompute(output) == report
    public = json.dumps(report, ensure_ascii=False)
    for scenario in dataset.corpus.scenarios:
        for turn in scenario.turns:
            assert turn.user_message not in public and turn.frozen_assistant_message not in public
    assert report["usage"] == {
        "actual_requests": 0,
        "input_tokens": "unknown",
        "output_tokens": "unknown",
    }
    assert len(report["scenarios"]) == 5
    raw = json.loads((output / "private-trace.json").read_text(encoding="utf-8"))
    raw["results"][0]["predictions"].pop()
    (output / "private-trace.json").write_text(json.dumps(raw), encoding="utf-8")
    (output / "trace-hash.json").write_text(json.dumps({"sha256": digest(raw)}), encoding="utf-8")
    with pytest.raises(ValueError, match="coverage"):
        recompute(output)
    (output / "status.json").write_text('{"status":"cancelled"}', encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete"):
        recompute(output)


async def test_campaign_cancellation_status(dataset, tmp_path, monkeypatch):
    async def cancel(*args):
        raise asyncio.CancelledError

    monkeypatch.setattr("personal_agent.evaluation.long_horizon.report.replay", cancel)
    with pytest.raises(asyncio.CancelledError):
        await evaluate(dataset, tmp_path / "cancelled")
    assert json.loads((tmp_path / "cancelled/status.json").read_text())["status"] == "cancelled"


async def test_campaign_failure_cannot_be_rescored(dataset, tmp_path, monkeypatch):
    from personal_agent.evaluation.long_horizon.models import ReplayResult

    async def fail(scenario):
        return ReplayResult(
            scenario_id=scenario.scenario_id,
            status="failed",
            predictions=[],
            snapshots=[],
            error_stage="routing",
            error_code="RuntimeError",
        )

    monkeypatch.setattr("personal_agent.evaluation.long_horizon.report.replay", fail)
    output = tmp_path / "failed"
    with pytest.raises(ValueError, match="incomplete"):
        await evaluate(dataset, output)
    status = json.loads((output / "status.json").read_text())
    assert status["status"] == "failed"
    assert status["errors"] == [{"stage": "routing", "code": "RuntimeError", "count": 5}]
    assert not (output / "summary.json").exists()
    with pytest.raises(ValueError, match="incomplete"):
        recompute(output)


def test_cli_selection_and_rejected_variants(dataset, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["benchmark", "--dataset", str(FIXTURES), "--list"])
    assert main() == 0 and "TR001" in capsys.readouterr().out
    for arguments in (["--variant", "llm"], ["--replay-mode", "oracle-history"], ["--live"]):
        monkeypatch.setattr("sys.argv", ["benchmark", *arguments])
        with pytest.raises(SystemExit):
            main()
    monkeypatch.setattr("sys.argv", ["benchmark", "--dataset", str(FIXTURES), "--split", "missing"])
    assert main() == 2
    with pytest.raises(ValidationError):
        Config(network=True)
