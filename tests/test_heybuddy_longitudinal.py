"""Offline long replay contracts; no live model, no real user state, no Gold as responses."""

import copy
import json
from pathlib import Path

import pytest

from personal_agent.core.models import ModelResponse, TextMessage, TokenUsage
from personal_agent.evaluation.heybuddy_bench.long_data import DATA, score, validate
from personal_agent.evaluation.heybuddy_bench.long_replay import run_cases
from personal_agent.evaluation.heybuddy_bench.longitudinal import carried_budget, select
from personal_agent.evaluation.heybuddy_bench.sequence_data import rows
from personal_agent.evaluation.heybuddy_bench.sequence_replay import SequenceContractProvider
from personal_agent.evaluation.runner import Budget

DATA = Path(__file__).resolve().parents[1] / DATA


def test_inventory_lengths_and_whole_family_split():
    result = validate(DATA)
    assert result["suite"] == 360 and result["long_scenarios"] == 120
    assert result["user_messages"] == 3120 and result["checkpoints"] == 1480
    dev, reserved = select(DATA, "dev"), select(DATA, "reserved")
    assert len(dev) == 84 and len(reserved) == 36
    assert not {(c["track"], c["family"]) for c in dev} & {
        (c["track"], c["family"]) for c in reserved
    }
    assert len(select(DATA, "short-reserved")) == 18
    assert len(select(DATA, "smoke")) == 3


def test_carried_budget_does_not_reset_previous_spend(tmp_path):
    protocol = json.loads((DATA / "live_protocol.json").read_text(encoding="utf-8"))
    (tmp_path / "session.json").write_text(json.dumps(protocol["carryover"]), encoding="utf-8")
    budget = carried_budget(tmp_path, protocol, "protocol-test")
    assert budget.requests == 113 and budget.reserved_cost == pytest.approx(0.427512)
    budget.reserve([TextMessage(role="user", text="synthetic request")], [], 100)
    budget.observe(TokenUsage(input_tokens=10, output_tokens=10))
    restored = carried_budget(tmp_path, protocol, "protocol-test")
    assert restored.requests == 114 and restored.reserved_cost > 0.427512
    changed = copy.deepcopy(protocol["carryover"])
    changed["pending"] = True
    (tmp_path / "session.json").write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match="Prior ledger"):
        carried_budget(tmp_path, protocol, "protocol-test")


class RecordingProvider(SequenceContractProvider):
    def __init__(self):
        self.inputs = []

    async def generate(self, messages, tools, **options):
        data = json.loads(messages[-1].text)
        self.inputs.append(data)
        response = await super().generate(messages, tools, **options)
        response.usage = TokenUsage(input_tokens=10, output_tokens=10)
        return response


async def test_learning_batches_include_every_intermediate_run_without_future_leakage(tmp_path):
    case = next(c for c in select(DATA, "smoke") if c["track"] == "memory")
    provider = RecordingProvider()
    result = (await run_cases([case], temp_parent=tmp_path, budget=Budget(), provider=provider))[0]
    assert result["error_code"] is None and len(result["steps"]) == 20
    assert len(provider.inputs) == 5
    assert [len(d["history"]) for d in provider.inputs] == [1, 4, 5, 5, 5]
    disclosed = [
        m["text"]
        for d in provider.inputs
        for r in d["history"]
        for m in r["messages"]
        if m["role"] == "user"
    ]
    assert disclosed == [s["text"] for s in case["input"]["steps"]]
    assert "已经拿到 offer" not in json.dumps(provider.inputs[0], ensure_ascii=False)
    assert all(
        not {"case_id", "family", "split", "checks", "expected"} & set(d) for d in provider.inputs
    )
    assert not list(tmp_path.iterdir())


class FailOnce(RecordingProvider):
    async def generate(self, messages, tools, **options):
        if not self.inputs:
            self.inputs.append(json.loads(messages[-1].text))
            return ModelResponse(
                text="invalid JSON", usage=TokenUsage(input_tokens=10, output_tokens=10)
            )
        return await super().generate(messages, tools, **options)


async def test_known_learning_failure_does_not_replace_state_or_retry_old_evidence(tmp_path):
    case = next(c for c in select(DATA, "smoke") if c["track"] == "memory")
    provider = FailOnce()
    result = (await run_cases([case], temp_parent=tmp_path, budget=Budget(), provider=provider))[0]
    assert len(result["steps"]) == 20
    assert result["steps"][0]["error_code"]
    assert len(provider.inputs) == 5
    first_source = provider.inputs[0]["history"][0]["run_id"]
    assert all(r["run_id"] != first_source for d in provider.inputs[1:] for r in d["history"])
    report = score([case], rows(DATA / "long/gold.jsonl"), [result])
    assert report["tracks"]["memory"]["passed"] == 0
    assert report["tracks"]["memory"]["checkpoints"] == 5


def test_missing_predictions_and_dynamic_target_reference_are_not_silently_passed():
    case = next(
        c
        for c in rows(DATA / "long/inputs.jsonl")
        if c["track"] == "routing" and c["case_id"] == "long-r-03-0"
    )
    report = score([case], rows(DATA / "long/gold.jsonl"), [])
    assert report["observed"] == 0 and report["tracks"]["routing"]["passed"] == 0
    assert report["tracks"]["routing"]["checkpoints"] == 28
    with pytest.raises(ValueError, match="Duplicate"):
        score([case], rows(DATA / "long/gold.jsonl"), [dict(case_id=case["case_id"])] * 2)


async def test_offline_scout_wiring_receives_twenty_real_source_runs(tmp_path):
    case = next(c for c in select(DATA, "smoke") if c["track"] == "proactive")
    provider = RecordingProvider()
    result = (await run_cases([case], temp_parent=tmp_path, budget=Budget(), provider=provider))[0]
    assert result["error_code"] is None
    assert len(result["steps"]) == 21  # Twenty user inputs plus one clock-only review.
    scouts = [
        d for d in provider.inputs if d.get("response_schema", {}).get("title") == "ScoutResult"
    ]
    assert len(scouts) == 20 and len({d["users"][0]["run_id"] for d in scouts}) == 20
    assert result["steps"][-1]["action"] == "none"
