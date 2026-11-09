"""Controlled context probes for attribution; never replace frozen benchmark questions."""

import copy
import json
from pathlib import Path

from personal_agent.evaluation.heybuddy_bench.dataset import (
    dump_rows,
    load_inputs,
    sha256,
)

ROOT = Path(__file__).resolve().parent


def main():
    originals = {
        c["case_id"]: c for folder in ["v0_4", "v0_4_challenge"] for c in load_inputs(ROOT / folder)
    }
    specs = [
        (
            "probe_plan_window",
            "challenge_pd_005",
            "notify",
            ["notify", "propose_task"],
            "At 18:00 today, please offer me the photo organization plan. "
            "Do not move any files without approval.",
        ),
        (
            "probe_auth_window",
            "custom_pd_009",
            "propose_task",
            ["notify", "propose_task"],
            "现在请先向我确认是否授权提交申请表；目前尚未授权，不要操作账户。",
        ),
        (
            "probe_work_resume",
            "public_028",
            "notify",
            ["notify"],
            "我已经帮完表弟，现在请提醒我接着写Alice的绩效评估改进项。",
        ),
        (
            "probe_plan_covered",
            "challenge_pd_005",
            "silent",
            ["silent"],
            "Thanks, I already received the organization plan. "
            "No follow-up needed; do not move any files.",
        ),
    ]
    cases, gold = [], []
    for cid, source, action, allowed, text in specs:
        case = copy.deepcopy(originals[source])
        case.update(
            case_id=cid,
            category="controlled_context_probe",
            scenario_group=cid,
            source=dict(type="custom", name="custom", derived_from=source),
        )
        body = case["input"]
        body["recent_user_evidence"].append(
            dict(id="probe-evidence", text=text, timestamp=body["now"])
        )
        candidate = body["candidates"][0]
        candidate["source_evidence_ids"].append("probe-evidence")
        cases.append(case)
        gold.append(
            dict(
                case_id=cid,
                track="proactive_decision",
                gold=dict(
                    action=action,
                    acceptable_actions=allowed,
                    proposal_ids=[candidate["id"]],
                    evidence_ids=["probe-evidence"],
                    as_of=body["now"],
                    next_review_at=None,
                ),
            )
        )
    folder = ROOT / "review_probes"
    folder.mkdir(exist_ok=True)
    dump_rows(folder / "inputs.jsonl", cases)
    dump_rows(folder / "gold.jsonl", gold)
    (folder / "manifest.json").write_text(
        json.dumps(
            dict(
                total=len(cases),
                purpose="Attribution only; excluded from headline scores",
                sha256={name: sha256(folder / name) for name in ["inputs.jsonl", "gold.jsonl"]},
            ),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    load_inputs(folder)


if __name__ == "__main__":
    main()
