"""Offline benchmark contracts; scripts cannot read gold or use the user's application state."""

import copy
import json
from pathlib import Path

import pytest

from personal_agent.core.models import ModelResponse, TokenUsage
from personal_agent.evaluation.heybuddy_bench.dataset import (
    dump_rows,
    load_inputs,
    read_rows,
    validate,
    validate_input,
)
from personal_agent.evaluation.heybuddy_bench.runner import ContractProvider, Meter, run_cases
from personal_agent.evaluation.heybuddy_bench.scoring import aggregate, score_case
from personal_agent.evaluation.runner import Budget, EvaluationLimit

DATA = Path(__file__).resolve().parents[1] / "evals/heybuddy_bench"


def test_frozen_jsonl_uses_lf_on_every_platform(tmp_path):
    path = tmp_path / "rows.jsonl"
    dump_rows(path, [{"case_id": "test", "text": "line1\nline2"}])
    assert b"\r\n" not in path.read_bytes()
    assert read_rows(path)[0]["text"] == "line1\nline2"


def test_frozen_data_and_source_provenance():
    result = validate(DATA)
    assert result["total"] == 150 and not result["human_reviewed"]
    cases = load_inputs(DATA)
    public = [r for r in cases if r["source"]["type"] == "public_derived"]
    assert len(public) == 35
    assert len({r["source"]["name"] for r in public}) == 5


@pytest.mark.parametrize("field", ["gold", "world_state", "has_answer", "expected_action"])
def test_reject_nested_leakage(field):
    row = copy.deepcopy(load_inputs(DATA)[0])
    row["input"]["history"][0][field] = "do not send"
    with pytest.raises(ValueError, match="Answer-bearing"):
        validate_input(row)


def test_missing_predictions_and_duplicate_ids_are_not_dropped():
    cases = load_inputs(DATA)[:2]
    gold = read_rows(DATA / "gold.jsonl")[:2]
    report = aggregate(cases, gold, [])
    assert report["tracks"]["thread_routing"]["decision_accuracy"] == 0
    assert report["expected"] == 2 and report["observed"] == 0
    row = dict(case_id=cases[0]["case_id"], prediction={"action": "new"}, usage={})
    with pytest.raises(ValueError, match="duplicate"):
        aggregate(cases, gold, [row, row])


async def test_input_only_runtime_isolation_and_no_network(tmp_path):
    cases = load_inputs(DATA)
    chosen = [
        next(c for c in cases if c["track"] == track)
        for track in ["thread_routing", "memory_learning", "proactive_decision"]
    ]
    # Metadata is deliberately poisoned: only the body may enter a model request.
    for case in chosen:
        case.update(category="SECRET_LABEL", source={"source_gold": "SECRET_LABEL"})

    class Spy(ContractProvider):
        async def generate(self, messages, tools, **options):
            assert not tools
            assert "SECRET_LABEL" not in str(messages)
            return await super().generate(messages, tools, **options)

    results = await run_cases(chosen, mode="smoke", temp_parent=tmp_path, provider=Spy())
    assert len(results) == 3
    assert all(
        r["error_code"] is None and r["prediction"]["action"] != "__error__" for r in results
    )
    assert all(r["usage"]["api_requests"] == 0 for r in results)
    assert not list(tmp_path.iterdir())


class WriteProvider(ContractProvider):
    async def generate(self, messages, tools, **options):
        data = json.loads(messages[-1].text)
        schema = data["response_schema"]["title"]
        if schema == "Extraction":
            source = data["history"][0]
            message = next(m for m in source["messages"] if m["role"] == "user")
            result = dict(
                candidates=[
                    dict(
                        category="preference",
                        subject="reply language",
                        content="默认简短中文回复。",
                        reason="Explicit preference",
                        confidence="confirmed",
                        evidence=[
                            dict(
                                conversation_id=source["conversation_id"],
                                run_id=source["run_id"],
                                sequence=message["sequence"],
                                quote=message["text"],
                            )
                        ],
                    )
                ]
            )
        elif schema == "Targets":
            result = dict(groups=[dict(path="human.md", candidate_ids=[1])])
        else:
            old = data["memory"]["human.md"]
            result = dict(
                action="update",
                old_text=old,
                new_text=old + "\n- 默认简短中文回复。",
                reason="Explicit preference",
            )
        return ModelResponse(text=json.dumps(result, ensure_ascii=False))


async def test_real_learning_writes_memory_and_receipts(tmp_path):
    case = next(c for c in load_inputs(DATA) if c["case_id"] == "custom_ml_011")
    result = (
        await run_cases([case], mode="smoke", temp_parent=tmp_path, provider=WriteProvider())
    )[0]
    assert result["error_code"] is None
    prediction = result["prediction"]
    assert prediction["action"] == "update", result
    assert prediction["receipts"][0]["state"] == "applied"
    assert "简短中文" in prediction["changes"][0]["after"]
    assert result["usage"]["provider_calls"] == 3
    assert not list(tmp_path.iterdir())


async def test_proactive_host_gate_avoids_call_and_does_not_send(tmp_path):
    case = copy.deepcopy(next(c for c in load_inputs(DATA) if c["case_id"] == "custom_pd_001"))
    case["input"]["now"] = "2026-11-05T23:30:00+08:00"
    result = (await run_cases([case], mode="smoke", temp_parent=tmp_path))[0]
    assert result["prediction"]["action"] == "silent"
    assert result["prediction"]["runtime"]["gate"] == "quiet_hours"
    assert result["usage"]["provider_calls"] == 0
    assert result["prediction"]["messages_sent"] == 0


async def test_invalid_output_and_budget_exhaustion_are_not_successes(tmp_path):
    class Invalid:
        async def generate(self, *args, **kwargs):
            return ModelResponse(text="not JSON")

    case = next(c for c in load_inputs(DATA) if c["track"] == "memory_learning")
    result = (await run_cases([case], mode="smoke", temp_parent=tmp_path, provider=Invalid()))[0]
    assert result["prediction"]["action"] == "__error__"
    assert result["prediction"]["changes"] == []
    result = (await run_cases([case], mode="smoke", temp_parent=tmp_path, budget=Budget(tokens=1)))[
        0
    ]
    assert result["prediction"]["action"] == "__error__"


async def test_usage_missing_is_unknown_and_stops_live():
    meter = Meter(ContractProvider(), Budget(), offline=False)
    from personal_agent.core.models import TextMessage

    messages = [TextMessage(role="system", text="JSON"), TextMessage(role="user", text="{}")]
    await meter.generate(messages, [], max_output_tokens=1024)
    assert meter.usage(meter.calls)["api_cost_usd"] is None
    assert meter.usage(meter.calls)["input_tokens"] is None
    with pytest.raises(EvaluationLimit, match="unknown_usage"):
        await meter.generate(messages, [], max_output_tokens=1024)


def test_new_filename_is_not_gold_leak_and_content_checks_are_separate():
    gold = dict(action="create", target_path="topics/arbitrary.md", content_checks=[["Python"]])
    prediction = dict(action="create", changes=[dict(path="topics/another.md", after="Python")])
    assert score_case("memory_learning", gold, prediction)["decision_correct"]
    assert score_case("memory_learning", gold, prediction)["content_check_pass"]


async def test_cost_does_not_double_count_reasoning():
    class Usage(ContractProvider):
        async def generate(self, *args, **kwargs):
            return ModelResponse(
                text="{}", usage=TokenUsage(input_tokens=10, output_tokens=20, reasoning_tokens=15)
            )

    from personal_agent.core.models import TextMessage

    meter = Meter(Usage(), Budget(input_price=1, output_price=2))
    await meter.generate([TextMessage(role="user", text="{}")], [], max_output_tokens=100)
    assert meter.usage(meter.calls)["api_cost_usd"] == 0.00005
