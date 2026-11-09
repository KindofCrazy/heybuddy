"""Revised scoring and carried real-call budgets use synthetic data only."""

import json
from pathlib import Path

import pytest

from personal_agent.evaluation.heybuddy_bench.cycle import carry_budget, selected_cases, summarize
from personal_agent.evaluation.heybuddy_bench.cycle import main as cycle_main
from personal_agent.evaluation.heybuddy_bench.dataset import read_rows, sha256, validate
from personal_agent.evaluation.heybuddy_bench.fact_scoring import score_facts
from personal_agent.evaluation.heybuddy_bench.state_scoring import score_state, valid_topic
from personal_agent.evaluation.heybuddy_bench.temporal_scoring import score_temporal
from personal_agent.evaluation.runner import EvaluationLimit

ROOT = Path(__file__).resolve().parents[1] / "evals/heybuddy_bench"


def test_revision_and_fresh_reserved_partition():
    assert validate(ROOT / "v0_3")["total"] == 150
    assert validate(ROOT / "v0_3_challenge", expected=10)["total"] == 30
    dev, reserved = selected_cases("development"), selected_cases("reserved")
    assert len(dev) == 165 and len(reserved) == 15
    assert not {c["case_id"] for c in dev}.intersection(c["case_id"] for c in reserved)
    assert not {json.dumps(c["input"], sort_keys=True) for c in dev}.intersection(
        json.dumps(c["input"], sort_keys=True) for c in reserved
    )


def test_v31_window_revision_changes_the_actual_visible_evidence():
    assert validate(ROOT / "v0_3_1")["total"] == 150
    old = {c["case_id"]: c for c in read_rows(ROOT / "v0_3/inputs.jsonl")}
    revised = {c["case_id"]: c for c in read_rows(ROOT / "v0_3_1/inputs.jsonl")}
    for number in (1, 2, 3, 17, 18, 30, 35):
        cid = f"custom_pd_{number:03}"
        texts = [e["text"] for e in revised[cid]["input"]["recent_user_evidence"]]
        assert any("16:00主动问我检查进度" in text for text in texts)
        assert revised[cid] != old[cid]
    assert sha256(ROOT / "gold.jsonl") == (
        "906586310897d3258e4a82153df8068384a7cc5e05e4392cdd5675fad4fa2e94"
    )


def state_gold():
    return dict(
        action="update",
        target_path="topics/exhibit.md",
        old_text="Exhibit is open.",
        new_text="Exhibit is cancelled.",
        content_checks=[["cancelled", "取消"]],
        optional_extra_paths=["human.md"],
    )


def test_multiple_supported_files_and_stale_fact_removal():
    gold = state_gold()
    changes = [
        dict(path="human.md", after="# Human\nLikes typography."),
        dict(path="topics/exhibit.md", after="Exhibit is cancelled."),
    ]
    prediction = dict(action="update", changes=changes)
    assert score_state("memory_learning", gold, prediction)["decision_correct"]
    changes[1]["after"] += "\nExhibit is open."
    result = score_state("memory_learning", gold, prediction)
    assert result["content_check_pass"] and not result["decision_correct"]
    assert not result["obsolete_text_removed"]


def test_wrong_subject_or_unrelated_file_cannot_pass_keyword_check():
    gold = state_gold()
    prediction = dict(
        action="update", changes=[dict(path="topics/other.md", after="Exhibit is cancelled.")]
    )
    assert not score_state("memory_learning", gold, prediction)["decision_correct"]
    prediction["changes"].insert(0, dict(path="topics/exhibit.md", after="Exhibit is cancelled."))
    assert not score_state("memory_learning", gold, prediction)["unrelated_files_preserved"]


def test_new_topic_path_is_validated_and_content_required():
    gold = dict(action="create", target_path="topics/notes.md", content_checks=[["Rust"]])
    prediction = dict(action="create", changes=[dict(path="projects/notes.md", after="Uses Rust")])
    assert score_state("memory_learning", gold, prediction)["decision_correct"]
    prediction["changes"][0]["after"] = "A different fact"
    assert not score_state("memory_learning", gold, prediction)["decision_correct"]
    assert not valid_topic("projects/../../escape.md")


def test_cross_file_scope_checks_real_final_state_and_preserves_global_default():
    gold = dict(
        action="update",
        target_path="topics/atlas.md",
        content_checks=[["Rust"]],
        optional_extra_paths=["human.md"],
        _memory_before={"human.md": "Uses Python globally."},
        fact_assertions=[dict(paths=["human.md"], all_any=[["Python"]])],
    )
    prediction = dict(
        action="update", changes=[dict(path="topics/atlas.md", after="Atlas uses Rust")]
    )
    assert score_facts("memory_learning", gold, prediction)["decision_correct"]
    prediction["changes"].append(dict(path="human.md", after="All projects use Rust"))
    result = score_facts("memory_learning", gold, prediction)
    assert not result["cross_file_assertions_pass"] and not result["decision_correct"]


def test_v32_only_changes_reviewed_labels_not_task_inputs():
    for part in ["development", "reserved"]:
        old = {c["case_id"]: c["input"] for c in selected_cases(part, "v0_3_1")}
        new = {c["case_id"]: c["input"] for c in selected_cases(part, "v0_3_2")}
        assert old == new


def test_future_review_cannot_reopen_today_do_not_disturb():
    gold = dict(
        action="silent",
        acceptable_actions=["silent", "defer"],
        proposal_ids=["one"],
        as_of="2026-11-08T18:00:00+08:00",
        review_not_before="2026-11-09T00:00:00+08:00",
    )
    prediction = dict(
        action="defer", proposal_ids=["one"], effective_next_review_at="2026-11-08T20:00:00+08:00"
    )
    assert not score_temporal("proactive_decision", gold, prediction)["decision_correct"]
    prediction["effective_next_review_at"] = "2026-11-09T09:00:00+08:00"
    assert score_temporal("proactive_decision", gold, prediction)["decision_correct"]
    prediction["proposal_ids"] = ["wrong"]
    assert not score_temporal("proactive_decision", gold, prediction)["decision_correct"]


def test_final_revision_keeps_every_canonical_label_acceptable():
    assert validate(ROOT / "v0_3_3")["total"] == 150
    assert validate(ROOT / "v0_3_3_challenge", expected=10)["total"] == 30
    rows = {r["case_id"]: r["gold"] for r in read_rows(ROOT / "v0_3_3/gold.jsonl")}
    assert rows["custom_pd_009"]["acceptable_actions"] == ["propose_task"]
    assert rows["custom_pd_010"]["acceptable_actions"] == ["propose_task"]


def test_v4_public_memory_is_old_user_state_without_changing_native_gold():
    assert validate(ROOT / "v0_4")["total"] == 150
    old_cases = {c["case_id"]: c for c in read_rows(ROOT / "v0_3_3/inputs.jsonl")}
    old_gold = {g["case_id"]: g for g in read_rows(ROOT / "v0_3_3/gold.jsonl")}
    new_gold = {g["case_id"]: g for g in read_rows(ROOT / "v0_4/gold.jsonl")}
    cases = {c["case_id"]: c for c in read_rows(ROOT / "v0_4/inputs.jsonl")}
    for number in range(9, 17):
        cid = f"public_{number:03}"
        assert cases[cid]["input"]["episode"] == old_cases[cid]["input"]["episode"]
        assert cases[cid]["source"] == old_cases[cid]["source"]
        assert (
            new_gold[cid]["provenance"]["native_gold"] == old_gold[cid]["provenance"]["native_gold"]
        )
        assert "topics/reference-notes.md" not in cases[cid]["input"]["memory_files"]
    notes = cases["public_009"]["input"]["memory_files"]["topics/running.md"]
    assert "27:12" in notes and "25:50" not in notes


def test_manifest_total_is_checked_against_actual_records(tmp_path):
    for name in ["inputs.jsonl", "gold.jsonl", "manifest.json"]:
        (tmp_path / name).write_bytes((ROOT / "v0_4_challenge" / name).read_bytes())
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["total"] = 150
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Manifest total mismatch"):
        validate(tmp_path, expected=10)


def test_published_starting_ledger_is_a_verified_historical_offset(tmp_path):
    protocol = json.loads((ROOT / "v0_3/cycle_protocol.json").read_text())
    protocol["carry_ledger"] = str(tmp_path / "unavailable-old-ledger.json")
    budget = carry_budget(tmp_path, protocol, "published-offset")
    assert budget.requests == 565
    assert budget.state()["cost_cny_upper_estimate"] == 1.875786
    assert budget.max_cost - budget.reserved_cost == 20


def test_diagnostic_probes_are_kept_out_of_main_and_challenge_scores():
    cases = selected_cases("diagnostic", "v0_4")
    assert len(cases) == 4 and all(c["case_id"].startswith("probe_") for c in cases)
    report = summarize(cases, [], "v0_4")
    assert set(report) == {"diagnostic"}
    assert report["diagnostic"]["expected"] == 4 and report["diagnostic"]["observed"] == 0


async def test_live_configuration_error_does_not_expose_input_or_retry(tmp_path, monkeypatch):
    from personal_agent import config

    def unavailable():
        raise OSError("synthetic-private-config-detail")

    monkeypatch.setattr(config, "load_settings", unavailable)
    monkeypatch.setattr(
        "sys.argv",
        [
            "cycle",
            "--phase",
            "config-check",
            "--revision",
            "v0_4",
            "--part",
            "smoke",
            "--live",
            "--root",
            str(tmp_path),
        ],
    )
    with pytest.raises(ValueError, match="Evaluation configuration is unavailable") as error:
        await cycle_main()
    assert "synthetic-private" not in str(error.value) and error.value.__suppress_context__
    ledger = json.loads((tmp_path / "session.json").read_text())
    assert ledger["requests"] == 565 and not ledger["pending"]


@pytest.mark.parametrize("pending", [True, False])
def test_budget_carries_prior_cost_and_unknown_state_blocks(tmp_path, pending):
    previous = tmp_path / "old.json"
    previous.write_text(
        json.dumps(
            dict(
                requests=7,
                input_tokens=1000000,
                output_tokens=500000,
                reasoning_tokens=0,
                cost_cny_upper_estimate=6,
                pending=pending,
                usage_complete=True,
            )
        )
    )
    protocol = dict(
        max_requests=100,
        max_tokens=10000000,
        max_cost_cny=20,
        input_price_cny=2,
        output_price_cny=8,
        carry_ledger=str(previous),
        carry_ledger_sha256=sha256(previous),
    )
    if pending:
        with pytest.raises(ValueError, match="uncertain"):
            carry_budget(tmp_path, protocol, "protocol")
        assert not (tmp_path / "session.json").exists()
    else:
        budget = carry_budget(tmp_path, protocol, "protocol")
        assert budget.requests == 7 and budget.reserved_cost == 6
        from personal_agent.core.models import TextMessage

        with pytest.raises(EvaluationLimit):
            budget.reserve([TextMessage(role="user", text="test")], [], 2000000)
