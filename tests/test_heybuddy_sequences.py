"""Offline contracts: no future/gold leakage, real state transitions, failure denominators."""

import copy
import json
from pathlib import Path

import pytest

from personal_agent.core.models import ModelResponse, TokenUsage
from personal_agent.evaluation.heybuddy_bench.sequence_data import DATA, rows, score, sha, validate
from personal_agent.evaluation.heybuddy_bench.sequence_replay import (
    SequenceContractProvider,
    run_sequences,
)
from personal_agent.evaluation.heybuddy_bench.sequences import select
from personal_agent.evaluation.runner import Budget

DATA = Path(__file__).resolve().parents[1] / DATA


def fixture(track, family, variant=0):
    return next(
        c
        for c in rows(DATA / "inputs.jsonl")
        if c["track"] == track and c["family"] == family and c["case_id"].endswith(f"-{variant}")
    )


def copy_data(tmp_path):
    for name in ("inputs.jsonl", "gold.jsonl", "manifest.json"):
        (tmp_path / name).write_bytes((DATA / name).read_bytes())


def rewrite(tmp_path, name, records):
    (tmp_path / name).write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8"
    )
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    manifest["sha256"][name] = sha(tmp_path / name)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_frozen_families_and_dev_only_selection():
    assert validate(DATA) == dict(
        total=60,
        families=30,
        splits=dict(dev=42, holdout=18),
        checkpoints=118,
        human_reviewed=False,
    )
    cases = rows(DATA / "inputs.jsonl")
    assert len(select(cases, "smoke")) == 3
    assert len(select(cases, "dev")) == 42
    assert all(c["split"] == "dev" for c in select(cases, "dev"))


@pytest.mark.parametrize("change", ["future", "gold_leak", "family_split"])
def test_validator_rejects_leakage_and_cross_split_families(tmp_path, change):
    copy_data(tmp_path)
    if change == "future":
        answers = rows(tmp_path / "gold.jsonl")
        answer = next(g for g in answers if g["case_id"] == "seq-p-01-0")
        answer["checks"][0]["opportunities"][0]["evidence"] = ["s2"]
        rewrite(tmp_path, "gold.jsonl", answers)
    else:
        inputs = rows(tmp_path / "inputs.jsonl")
        if change == "gold_leak":
            inputs[0]["input"]["steps"][0]["expected_action"] = "select"
        else:
            inputs[0]["split"] = "holdout"
        rewrite(tmp_path, "inputs.jsonl", inputs)
    with pytest.raises(ValueError):
        validate(tmp_path)


def test_missing_and_failed_predictions_stay_in_denominator():
    cases = select(rows(DATA / "inputs.jsonl"), "smoke")
    report = score(cases, rows(DATA / "gold.jsonl"), [])
    assert report["expected"] == 3 and report["observed"] == 0
    assert all(t["scenarios_passed"] == 0 for t in report["tracks"].values())
    with pytest.raises(ValueError, match="Duplicate"):
        score(cases, rows(DATA / "gold.jsonl"), [dict(case_id="duplicate")] * 2)


class RecordingProvider(SequenceContractProvider):
    def __init__(self):
        self.inputs = []

    async def generate(self, messages, tools, **options):
        self.inputs.append(json.loads(messages[-1].text))
        response = await super().generate(messages, tools, **options)
        response.usage = TokenUsage(input_tokens=10, output_tokens=10)
        return response


async def test_learning_discloses_only_past_and_no_metadata_or_labels(tmp_path):
    case = fixture("memory", "status_transition")
    provider = RecordingProvider()
    result = (
        await run_sequences([case], temp_parent=tmp_path, budget=Budget(), provider=provider)
    )[0]
    assert result["error_code"] is None and len(result["steps"]) == 2
    assert len(provider.inputs) == 2  # Empty extractor causes no consolidation calls.
    assert "准备入职" not in json.dumps(provider.inputs[0], ensure_ascii=False)
    assert "准备入职" in json.dumps(provider.inputs[1], ensure_ascii=False)
    for data in provider.inputs:
        for key in ("case_id", "family", "split", "checks", "gold", "expected_action"):
            assert key not in data
    assert not list(tmp_path.iterdir())


class MemoryWriter(RecordingProvider):
    async def generate(self, messages, tools, **options):
        data = json.loads(messages[-1].text)
        self.inputs.append(data)
        schema = data["response_schema"]["title"]
        if schema == "Extraction":
            source = data["history"][0]
            message = next(m for m in source["messages"] if m["role"] == "user")
            self.content = (
                "正在申请 Agent 实习。"
                if "正在申请" in message["text"]
                else "已获 offer，准备入职。"
            )
            result = dict(
                candidates=[
                    dict(
                        category="preference",
                        subject="internship",
                        content=self.content,
                        reason="Explicit user update",
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
            result = dict(
                action="update",
                old_text=data["memory"]["human.md"],
                new_text="# 用户\n\n" + self.content,
                reason="Replace current state",
            )
        return ModelResponse(
            text=json.dumps(result, ensure_ascii=False),
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )


async def test_learning_persists_prior_state_and_replaces_it_next_checkpoint(tmp_path):
    case = fixture("memory", "status_transition")
    provider = MemoryWriter()
    result = (
        await run_sequences([case], temp_parent=tmp_path, budget=Budget(), provider=provider)
    )[0]
    assert not result["error_code"], result
    assert len(result["steps"]) == 2
    assert "正在申请" in result["steps"][0]["files"]["human.md"]
    assert "准备入职" in result["steps"][1]["files"]["human.md"]
    assert "正在申请" not in result["steps"][1]["files"]["human.md"]
    consolidations = [
        d for d in provider.inputs if d["response_schema"]["title"] not in {"Extraction", "Targets"}
    ]
    assert "正在申请" in consolidations[1]["memory"]["human.md"]
    assert result["usage"]["provider_calls"] == 6


async def test_router_exercises_candidate_miss_and_thread_filtered_history(tmp_path):
    cases = [fixture("routing", "old_exact", 1), fixture("routing", "scope_isolation", 1)]
    results = await run_sequences(cases, temp_parent=tmp_path, budget=Budget())
    missed, active = [r["steps"][0] for r in results]
    assert not any(r["error_code"] for r in results), results
    assert len(missed["candidates"]) == 24 and "research" not in missed["candidates"]
    assert "research" not in missed["model_candidates"]
    assert active["thread_id"] == "research"
    assert "SQLite" in active["context"] and "INTERVIEW_ONLY" not in active["context"]
    assert "research" in active["effective_candidates"]


class ScoutProvider(RecordingProvider):
    async def generate(self, messages, tools, **options):
        data = json.loads(messages[-1].text)
        schema = data.get("response_schema", {}).get("title")
        if schema != "ScoutResult":
            return await super().generate(messages, tools, **options)
        self.inputs.append(data)
        user = data["users"][0]
        source = {k: user[k] for k in ("conversation_id", "run_id", "sequence")}
        source["quote"] = user["text"]
        if data["existing"]:
            result = dict(
                changes=[],
                followups=[
                    dict(
                        proposal_id=p["id"],
                        proposal_revision=p["revision"],
                        outcome="covered",
                        sources=[source],
                    )
                    for p in data["existing"]
                    if p.get("needs_followup_review")
                ],
            )
        else:
            result = dict(
                changes=[
                    dict(
                        topic_key="benchmark",
                        title="Benchmark",
                        context=user["text"],
                        sources=[source],
                    )
                ],
                followups=[],
            )
        return ModelResponse(
            text=json.dumps(result, ensure_ascii=False),
            usage=TokenUsage(input_tokens=10, output_tokens=10),
        )


async def test_real_scout_creates_opportunity_then_coverage_prevents_attention(tmp_path):
    case = fixture("proactive", "already_covered")
    provider = ScoutProvider()
    result = (
        await run_sequences([case], temp_parent=tmp_path, budget=Budget(), provider=provider)
    )[0]
    assert not result["error_code"], result
    assert len(result["steps"]) == 3
    first, covered, after = result["steps"]
    assert first["proposals"][0]["event_ids"] == ["s1"]
    assert covered["proposals"][0]["followup_state"] == "covered"
    assert covered["proposals"][0]["status"] == "open"
    assert after["action"] == "none" and len(provider.inputs) == 2
    report = score([case], rows(DATA / "gold.jsonl"), [result])
    assert report["tracks"]["proactive"]["checkpoints_passed"] == 3


async def test_attention_is_attempted_once_and_decision_payload_is_read(tmp_path):
    case = fixture("proactive", "raw_discovery")
    provider = ScoutProvider()
    result = (
        await run_sequences([case], temp_parent=tmp_path, budget=Budget(), provider=provider)
    )[0]
    assert not result["error_code"], result
    assert result["steps"][1]["action"] == "silent"
    assert (
        sum(
            d.get("response_schema", {}).get("title") == "AttentionDecision"
            for d in provider.inputs
        )
        == 1
    )


async def test_budget_exhaustion_keeps_partial_sequence_and_stops_new_cases(tmp_path):
    case = fixture("memory", "status_transition")
    result = await run_sequences(
        [case, copy.deepcopy(case)],
        temp_parent=tmp_path,
        budget=Budget(requests=1),
        provider=RecordingProvider(),
    )
    assert len(result) == 1
    report = score([case], rows(DATA / "gold.jsonl"), result)
    assert report["tracks"]["memory"]["checkpoints"] == 2
    assert report["tracks"]["memory"]["scenarios_passed"] == 0
