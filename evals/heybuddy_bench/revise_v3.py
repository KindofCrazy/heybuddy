"""Deterministically build a new revision; never overwrite v0.2 or inspect predictions."""

import copy
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from personal_agent.evaluation.heybuddy_bench.dataset import dump_rows, read_rows, sha256, validate

ROOT = Path(__file__).resolve().parent
NOW = "2026-11-08T18:00:00+08:00"
BEFORE = "2026-11-08T17:00:00+08:00"


def record(cid, track, body, label, family):
    return (
        dict(
            case_id=cid,
            track=track,
            input=body,
            category=family,
            scenario_group=family,
            source=dict(type="custom", name="custom"),
            review_status="agent_reviewed",
        ),
        dict(case_id=cid, track=track, gold=label),
    )


def memory_case(cid, text, files, target, old, new, aliases, family, **extra):
    body = dict(
        now=NOW,
        memory_files=files,
        memory_catalogue=list(files),
        episode=dict(
            id="episode",
            thread_id=None,
            events=[dict(id="event", speaker="user", text=text, timestamp=BEFORE)],
        ),
    )
    label = dict(
        action="update",
        target_path=target,
        old_text=old,
        new_text=new,
        evidence_ids=["event"],
        content_checks=aliases,
        **extra,
    )
    return record(cid, "memory_learning", body, label, family)


def memory_challenges():
    specs = [
        (
            "cancellation",
            "The Saturday robotics exhibition is cancelled. The robotics class still runs.",
            "exhibition",
            "Saturday exhibition is scheduled.",
            "Saturday exhibition is cancelled.",
            [["cancelled", "canceled", "取消"]],
        ),
        (
            "scoped_preference",
            "For the Atlas project only, use Rust now. Keep Python for my other projects.",
            "atlas",
            "Atlas uses Python.",
            "Atlas uses Rust.",
            [["Rust"]],
        ),
        (
            "deadline",
            "The grant submission moved from November 12 to November 19; use the new date.",
            "grant",
            "Grant submission deadline: November 12.",
            "Grant submission deadline: November 19.",
            [["November 19", "11-19", "11月19"]],
        ),
        (
            "knowledge",
            "Correct my CLI notes: the dry-run flag is --preview, not --check. "
            "Save this correction.",
            "cli",
            "Dry-run flag: --check.",
            "Dry-run flag: --preview.",
            [["--preview"]],
        ),
        (
            "progress",
            "For my oak desk restoration, sanding is finished; the next step is oiling.",
            "desk",
            "Desk restoration: sanding in progress.",
            "Desk restoration: oiling next.",
            [["oiling", "上油"], ["finished", "完成"]],
        ),
        (
            "entity_isolation",
            "周末合唱演出取消了，合唱排练仍按原计划进行。",
            "concert",
            "周末演出照常举行。",
            "周末演出取消。",
            [["取消", "cancel"]],
        ),
        (
            "conditional_scope",
            "只在海报项目中不要用橙色，我的博客主题仍用橙色。请更新海报配色。",
            "poster",
            "海报主色：橙色。",
            "海报避免橙色。",
            [["避免", "不用", "不要", "不使用"]],
        ),
        (
            "rescheduled",
            "My rehearsal moved to Tuesday, November 17. It is no longer on Monday, November 16.",
            "rehearsal",
            "Rehearsal: Monday, November 16.",
            "Rehearsal: Tuesday, November 17.",
            [["November 17", "11-17", "11月17"]],
        ),
        (
            "explicit_note",
            "请更正我的摄影笔记：这个相机的最高同步快门是 1/200 秒，不是 1/250 秒。",
            "camera",
            "最高同步快门：1/250 秒。",
            "最高同步快门：1/200 秒。",
            [["1/200"]],
        ),
        (
            "two_facts",
            "The garden irrigation leak is fixed. "
            "Keep the weekly filter cleaning reminder in my notes.",
            "garden",
            "Irrigation leak: open.\nFilter cleaning: weekly.",
            "Irrigation leak: fixed.\nFilter cleaning: weekly.",
            [["fixed", "修好", "修复"], ["weekly", "每周"]],
        ),
    ]
    result = []
    for n, (family, text, stem, old, new, aliases) in enumerate(specs):
        path = f"topics/{stem}.md"
        files = {
            "human.md": "# Human\n- Uses Python for general scripts.\n",
            path: "# Notes\n" + old,
        }
        files["topics/unrelated.md"] = (
            "# Separate activity\n- Keep the ordinary class and blog unchanged.\n"
        )
        result.append(
            memory_case(
                f"challenge_ml_{n + 1:03}",
                text,
                files,
                path,
                old,
                new,
                aliases,
                family,
                preserve_terms=["weekly"] if family == "two_facts" else [],
            )
        )
    return result


def routing_challenges():
    specs = [
        (
            "lexical_collision",
            ["Machine inventory", "Vision training"],
            ["Ordering spare machines", "Awaiting GPU allocation for augmentation experiments"],
            "The machine slot is available; resume the augmentation experiments.",
            "select",
        ),
        (
            "shared_suffix",
            ["Garden schedule", "Garden budget"],
            ["Watering on Saturdays", "Waiting for nursery price quotes"],
            "The nursery quote arrived. Let's resume comparing the soil costs.",
            "select",
        ),
        (
            "ambiguous_active",
            ["Grant draft", "Workshop draft"],
            ["Reviewing the revised version", "Reviewing the revised version"],
            "Please use the revised version we discussed; I haven't specified which draft.",
            "clarify",
        ),
        (
            "new_entity",
            ["Atlas migration", "Boreal migration"],
            ["Database indexing", "Database indexing"],
            "A separate project, Cedar, also needs indexing. Start a new discussion for it.",
            "new",
        ),
        (
            "ack",
            ["Camera repair", "Camera purchase"],
            ["Checking the shutter", "Comparing models"],
            "收到。",
            "continue",
        ),
        (
            "homograph",
            ["课题申请", "课程设计"],
            ["正在等待预算审批", "正在写实验指导书"],
            "课程的实验指导书要补上示意图，我们接着改那份指导书。",
            "select",
        ),
        (
            "implicit_resume",
            ["海报排版", "图册排版"],
            ["在修改活动日期", "正在校对照片说明"],
            "照片说明校对完了，继续整理图册的页码。",
            "select",
        ),
        (
            "two_referents",
            ["周末采购清单", "远足装备清单"],
            ["有一个新版本", "有一个新版本"],
            "那份清单的新版本改好了吗？",
            "clarify",
        ),
        (
            "different_goal",
            ["旧书整理", "书架组装"],
            ["按作者分类", "安装最后一层"],
            "这次是另一件事：我想筹办一次旧书义卖，单独开个话题。",
            "new",
        ),
        (
            "social",
            ["发票归档", "周报整理"],
            ["等待发票号码", "等待进度更新"],
            "今天夕阳真漂亮，想随便聊聊。",
            "chat",
        ),
    ]
    result = []
    for n, (family, titles, states, text, action) in enumerate(specs):
        ids = [str(uuid5(NAMESPACE_URL, f"hb-v3/{family}/{j}")) for j in range(2)]
        body = dict(
            now=NOW,
            active_thread_id=ids[0],
            current_message=text,
            thread_candidates=[
                dict(id=i, title=t, current_state=s, last_active_at=BEFORE)
                for i, t, s in zip(ids, titles, states, strict=True)
            ],
            history=[
                dict(episode_id=str(j), timestamp=BEFORE, thread_id=i, messages=[s])
                for j, (i, s) in enumerate(zip(ids, states, strict=True))
            ],
            recent_context=[dict(timestamp=BEFORE, thread_id=ids[0], text=states[0])],
        )
        label = dict(
            action=action,
            thread_id=ids[1] if action == "select" else ids[0] if action == "continue" else None,
        )
        result.append(record(f"challenge_rt_{n + 1:03}", "thread_routing", body, label, family))
    return result


def proactive_challenges():
    specs = [
        (
            "competing",
            "Please check my poster deadline now at 18:00; the poster is still unfinished.",
            "My seed order is due next week. No need to check it today.",
            "notify",
            0,
        ),
        (
            "dnd",
            "Do not contact me proactively today, including about the poster.",
            "The poster is not finished yet.",
            "silent",
            0,
        ),
        (
            "too_early",
            "Check my poster progress at 20:30, not earlier. Deadline is 21:00.",
            "My seed order is due next week; don't contact me about that today.",
            "defer",
            0,
        ),
        (
            "finished_first",
            "The poster is complete; no follow-up needed.",
            "Please check the still-open seed order now at 18:00; I need to send it by 18:30.",
            "notify",
            1,
        ),
        (
            "permission",
            "Suggest a plan now to move my photos to a new folder, "
            "but do not move any files without approval.",
            "No follow-up needed on the completed poster.",
            "propose_task",
            0,
        ),
        (
            "priority",
            "演出海报已经完成，不用再问。",
            "请在现在18点问一下还没发出的报名表，18点半必须发出。",
            "notify",
            1,
        ),
        (
            "focused",
            "接下来到20点我要专注写稿，暂时不要打扰，20点后再问海报进度。",
            "报名表已经发出，不用再问。",
            "defer",
            0,
        ),
        (
            "covered",
            "我刚刚已经告诉你海报完成了，这件事不再跟进。",
            "报名表也已经发出，不用再问。",
            "silent",
            0,
        ),
        (
            "concrete_help",
            "请在18点提醒我检查还未发出的展览邀请，现在就是检查窗口。",
            "下周的采购计划今天不需要跟进。",
            "notify",
            0,
        ),
        (
            "explicit_refusal",
            "今天不要主动找我聊展览，明天我会自己来找你。",
            "采购也不需要跟进，请不要主动联系。",
            "silent",
            0,
        ),
    ]
    result = []
    for n, (family, first, second, action, selected) in enumerate(specs):
        events = [dict(id=f"e{j}", text=t, timestamp=BEFORE) for j, t in enumerate([first, second])]
        proposals = [
            dict(
                id=f"proposal-{j}",
                title="Current follow-up",
                topic_key=f"topic-{j}",
                context=t,
                status="open",
                due_at="2026-11-08T21:00:00+08:00",
                source_evidence_ids=[f"e{j}"],
                last_notified_at=None,
                followup_state="pending",
            )
            for j, t in enumerate([first, second])
        ]
        body = dict(
            now=NOW,
            recent_user_evidence=events,
            candidates=proposals,
            attention_context=dict(availability="unknown", explicit_do_not_disturb=False),
        )
        label = dict(
            action=action,
            proposal_ids=[f"proposal-{selected}"],
            evidence_ids=[f"e{selected}"],
            as_of=NOW,
            next_review_at="2026-11-08T20:30:00+08:00" if action == "defer" else None,
        )
        result.append(record(f"challenge_pd_{n + 1:03}", "proactive_decision", body, label, family))
    return result


def publish(directory, pairs):
    directory.mkdir(exist_ok=True)
    cases, gold = map(list, zip(*pairs, strict=True))
    dump_rows(directory / "inputs.jsonl", cases)
    dump_rows(directory / "gold.jsonl", gold)
    (directory / "manifest.json").write_text(
        json.dumps(
            dict(
                version="0.3.0",
                human_reviewed=False,
                sha256={name: sha256(directory / name) for name in ["inputs.jsonl", "gold.jsonl"]},
            ),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    validate(directory, expected=len(pairs) // 3)


def main():
    cases, labels = read_rows(ROOT / "inputs.jsonl"), read_rows(ROOT / "gold.jsonl")
    gold = {r["case_id"]: r for r in labels}
    alternatives = json.loads((ROOT / "live_protocol.json").read_text(encoding="utf-8"))[
        "review_alternatives"
    ]
    audit, pairs = [], []
    replacements = [
        memory_case(
            f"temporary-{n}",
            text,
            {"human.md": "# Human\n", f"topics/{name}.md": "# Notes\n" + old},
            f"topics/{name}.md",
            old,
            new,
            [[term]],
            "explicit_personal_note",
        )
        for n, (name, text, old, new, term) in enumerate(
            [
                (
                    "backup",
                    "Correct my backup notes: the NAS address is 10.0.0.42, not 10.0.0.24.",
                    "NAS address: 10.0.0.24.",
                    "NAS address: 10.0.0.42.",
                    "10.0.0.42",
                ),
                (
                    "reading",
                    "My reading club has moved to the library room B2. Update my club note.",
                    "Reading club meets in room A1.",
                    "Reading club meets in room B2.",
                    "B2",
                ),
                (
                    "printing",
                    "Please correct my printer note: use A5 paper for these zines, not A4.",
                    "Zine paper: A4.",
                    "Zine paper: A5.",
                    "A5",
                ),
                (
                    "studio",
                    "Do not store any studio access code; "
                    "just note that access is now arranged through reception.",
                    "Studio access: contact the owner.",
                    "Studio access: through reception.",
                    "reception",
                ),
            ]
        )
    ]
    for case in copy.deepcopy(cases):
        cid = case["case_id"]
        row = copy.deepcopy(gold[cid])
        if case["source"].get("name") == "MemoryAgentBench":
            replacement = replacements.pop(0)
            replacement[0]["case_id"] = replacement[1]["case_id"] = "replacement_" + cid
            audit.append(
                dict(
                    case_id=cid,
                    revision="replace",
                    replacement=replacement[0]["case_id"],
                    reason="Fictional-world relations are diagnostic, not primary personal memory",
                )
            )
            pairs.append(replacement)
            continue
        if case["source"].get("name") == "TIAGE":
            row["gold"]["acceptable_actions"] = sorted(
                set(
                    row["gold"].get("acceptable_actions", [row["gold"]["action"]])
                    + ["chat", "continue"]
                )
            )
            audit.append(
                dict(
                    case_id=cid,
                    revision="label",
                    reason="Topic shift is not necessarily a new task",
                )
            )
        if cid in alternatives:
            row["gold"]["storage_alternatives"] = alternatives[cid]
            audit.append(
                dict(
                    case_id=cid,
                    revision="storage",
                    reason="Previously reviewed Core/project alternatives",
                )
            )
        if (
            case["track"] == "proactive_decision"
            and row["gold"]["action"] == "notify"
            and cid.startswith("custom_pd_")
        ):
            body = case["input"]
            for event in body["recent_user_evidence"]:
                event["text"] = event["text"].replace(
                    "临近时可以问我检查进度", f"请在{body['now'][:10]}的16:00问我检查进度"
                )
            audit.append(
                dict(
                    case_id=cid,
                    revision="input",
                    reason="Explicit 16:00 check window replaces ambiguous proximity",
                )
            )
        if cid == "public_016":
            row["gold"]["optional_extra_paths"] = ["human.md"]
            audit.append(
                dict(
                    case_id=cid,
                    revision="scoring",
                    reason="Allow independently evidenced learning interest",
                )
            )
        pairs.append((case, row))
    publish(ROOT / "v0_3", pairs)
    # Replacement development cases are not duplicated into the fresh reserved challenge.
    challenges = routing_challenges() + memory_challenges() + proactive_challenges()
    publish(ROOT / "v0_3_challenge", challenges)
    (ROOT / "v0_3/revisions.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    protocol_path = ROOT / "v0_3/cycle_protocol.json"
    if not protocol_path.exists():
        carry = Path(".heybuddy/evaluations/hb-live-20261107/session.json")
        protocol_path.write_text(
            json.dumps(
                dict(
                    version="20261108-cycle-v1",
                    model="deepseek-flash",
                    endpoint_host="api.deepseek.com",
                    max_cost_cny=21.875786,
                    round_budget_cny=20,
                    max_requests=6000,
                    max_tokens=30000000,
                    input_price_cny=2,
                    output_price_cny=8,
                    price_source="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
                    carry_ledger=carry.as_posix(),
                    carry_ledger_sha256=sha256(carry),
                    carry_cost_cny=1.875786,
                    data_hashes={
                        f"{folder}/{name}": sha256(ROOT / folder / name)
                        for folder in ["v0_3", "v0_3_challenge"]
                        for name in ["inputs.jsonl", "gold.jsonl", "manifest.json"]
                    },
                    scoring_sha256=sha256(
                        Path("src/personal_agent/evaluation/heybuddy_bench/state_scoring.py")
                    ),
                    reserved="challenge numbers 006-010 in every track; "
                    "run after final code freeze",
                    annotation="Agent authored/reviewed; "
                    "not independent human or third-party blind",
                ),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )


if __name__ == "__main__":
    main()
