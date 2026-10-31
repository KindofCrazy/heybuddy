from types import SimpleNamespace

import pytest

from personal_agent.core import prompts as core_prompts
from personal_agent.core.prompts import CONVERSATION_PROMPT
from personal_agent.evaluation import flow_runtime
from personal_agent.evaluation.flow_cases import ORDER, flow_cases, validate_contract
from personal_agent.evaluation.flow_revision_cases import ORDER as REVISION_ORDER
from personal_agent.evaluation.flow_revision_cases import revision_cases
from personal_agent.evaluation.prompt_audit import stages
from personal_agent.organization.models import OrganizationError
from personal_agent.proactive.composer import Composer, MessageDraft


@pytest.mark.parametrize("split", ["flow-dev", "flow-positive", "flow-holdout"])
def test_all_production_call_sites_have_fixed_cases(split):
    cases = flow_cases(split)
    assert tuple(cases) == ORDER and len(cases) == 19
    assert set(cases) <= stages().keys()
    assert {"question", "candidates"} <= cases["retrieval"]["input"].keys()
    assert "memory" in cases["curator"]["input"]


def test_empty_target_groups_are_not_a_valid_rejection():
    case = flow_cases("flow-dev")["selection"]
    with pytest.raises(ValueError, match="coverage"):
        validate_contract("selection", {"groups": [], "paths": []}, case)
    validate_contract(
        "selection",
        {
            "groups": [{"path": "human.md", "candidate_ids": [1]}],
            "paths": [],
        },
        case,
    )


@pytest.mark.parametrize(
    "split", ["revision-dev", "revision-control", "revision-held-a", "revision-held-b"]
)
def test_revision_cases_keep_the_same_host_envelopes_and_positive_controls(split):
    cases = revision_cases(split)
    assert tuple(cases) == REVISION_ORDER
    assert "plain_user" in cases["main"]
    assert cases["episode"]["input"]["runs"]
    assert cases["thread"]["input"]["existing_threads"][0]["id"] == "topic"
    assert cases["repair"]["input"]["previous_rebuild"]
    assert cases["compaction"]["input"]["summary_limits"]["max_serialized_utf8_bytes"] == 1024


def test_compaction_budget_counts_json_utf8_not_characters():
    case = flow_cases("flow-dev")["compaction"]
    validate_contract("compaction", "甲" * 100, case)
    with pytest.raises(ValueError, match="summary size"):
        validate_contract("compaction", "甲" * 400, case)


def test_partition_checks_use_the_real_episode_validator():
    case = flow_cases("flow-dev")["episode"]
    ids = [r["run_id"] for r in case["input"]["runs"]]
    result = {
        "episodes": [
            {"run_ids": ids, "title": "合成讨论", "summary": "讨论计划。", "highlights": []}
        ]
    }
    validate_contract("episode", result, case)
    result["episodes"][0]["run_ids"] = ids[:-1]
    with pytest.raises(OrganizationError):
        validate_contract("episode", result, case)


def test_an_edit_must_match_the_current_target_exactly_once():
    case = flow_cases("flow-positive")["consolidation"]
    with pytest.raises(ValueError, match="exact update"):
        validate_contract(
            "consolidation",
            {
                "action": "update",
                "old_text": "not in the supplied file",
                "new_text": "new",
            },
            case,
        )


async def test_runtime_requires_live_before_loading_credentials(tmp_path, monkeypatch):
    def forbidden():
        raise AssertionError("Offline entry must not load credentials")

    monkeypatch.setattr(flow_runtime, "load_settings", forbidden)
    with pytest.raises(ValueError, match="Explicit --live"):
        await flow_runtime.run(SimpleNamespace(live=False, output=tmp_path))


async def test_composer_keeps_character_style_without_main_turn_instructions():
    class Model:
        async def ask(self, schema, prompt, data):
            assert data["persona_style"] == [{"role": "system", "text": "角色口吻"}]
            return MessageDraft(text="祝顺利")

    original = [{"role": "system", "text": "角色口吻\n\n" + CONVERSATION_PROMPT}]
    await Composer(Model()).compose({}, original, "2026-10-31")
    assert original[0]["text"].endswith(CONVERSATION_PROMPT)


def test_packaged_conversation_prompt_is_stable_across_checkout_line_endings(tmp_path, monkeypatch):
    folder = tmp_path / "package"
    (folder / "prompts").mkdir(parents=True)
    monkeypatch.setattr(core_prompts, "__file__", str(folder / "core" / "prompts.py"))
    path = folder / "prompts" / "conversation.md"
    path.write_bytes("第一段\r\n第二段\r\n".encode())
    windows = core_prompts._load_conversation_prompt()
    path.write_bytes("第一段\n第二段\n".encode())
    assert windows == core_prompts._load_conversation_prompt() == "第一段\n第二段"


async def test_runtime_and_probe_share_the_same_request_cap(tmp_path):
    (tmp_path / "ledger.jsonl").write_text("{}\n" * 100, encoding="utf-8")

    class ForbiddenProvider:
        async def generate(self, *args, **kwargs):
            raise AssertionError("No API calls after budget exhaustion")

    meter = flow_runtime.Meter(ForbiddenProvider(), tmp_path, "runtime", 100)
    with pytest.raises(ValueError, match="budget exhausted"):
        await meter.generate([], [])
