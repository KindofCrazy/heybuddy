"""Rebuild frozen synthetic sequences. No API, private data, or generated model labels."""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_case(track, family, number, variant, body, checks):
    identifier = f"seq-{track[:1]}-{number:02d}-{variant}"
    split = "holdout" if number >= 8 else "dev"
    return (
        dict(case_id=identifier, track=track, family=family, split=split, input=body),
        dict(case_id=identifier, checks=checks),
    )


def routing_cases():
    for n, family in enumerate(
        [
            "old_exact",
            "old_paraphrase",
            "interleave",
            "shared_keyword",
            "new_project",
            "ambiguous_reference",
            "scope_isolation",
            "explicit_id",
            "state_tail",
            "active_bias",
        ],
        1,
    ):
        for v in (0, 1):
            count = 8 if v == 0 else 55
            threads = [
                dict(
                    id="research",
                    title="空间推理研究",
                    current_state="视频生成增强空间推理；只用 SQLite，不引入 Redis。",
                    last_active_at="2026-01-01T10:00:00+08:00",
                ),
                dict(
                    id="interview",
                    title="实习面试",
                    current_state="准备 Agent 算法岗面试；需要复习 GRPO。",
                    last_active_at="2026-02-01T10:00:00+08:00",
                ),
            ]
            threads += [
                dict(
                    id=f"other-{i:02d}",
                    title=f"独立事务 {i:02d}",
                    current_state=f"只处理事务 {i:02d}，与科研和面试无关。",
                    last_active_at=f"2026-03-{i // 20 + 1:02d}T{8 + i % 12:02d}:00:00+08:00",
                )
                for i in range(count)
            ]
            active = "interview"
            text, actions, target = "继续之前的空间推理研究，补一些样本。", ["select"], "research"
            if n == 2:
                text = "继续三个月前那个用生成视频提升三维理解的研究。"
            elif n == 3:
                text = "先继续 Agent 算法岗的准备。"
                actions, target = ["continue"], "interview"
            elif n == 4:
                threads[1].update(title="空间推理面试题", current_state="面试练习，不是论文实验。")
                text = "回到做论文实验的那个项目，继续加数据。"
            elif n == 5:
                text, actions, target = (
                    "新开一个独立的空间推理项目，旧项目保留，不延续原任务。",
                    ["new"],
                    None,
                )
            elif n == 6:
                threads[1].update(title="另一个科研项目", current_state="也需要补评测样本。")
                text, actions, target = "之前那个项目再补点数据。", ["clarify"], None
            elif n == 7:
                active = "research"
                text, actions, target = (
                    "继续这个空间推理研究，沿用先前的数据约束。",
                    ["continue"],
                    "research",
                )
            elif n == 8:
                text = (
                    "继续 thread:research 的任务。"
                    if v == 0
                    else "切换到 research 继续空间推理研究。"
                )
            elif n == 9:
                threads[0].update(
                    title="研究甲",
                    current_state="背景资料。" * (10 if v == 0 else 200)
                    + "当前唯一任务：视频生成增强空间推理。",
                )
                text = "回到那个视频生成增强空间推理的任务。"
            elif n == 10:
                active = "research" if v == 0 else "interview"
                text = "继续用生成的视频做三维能力评测，暂时不聊面试。"
                actions = ["continue"] if active == "research" else ["select"]
            steps = [dict(id="s1", at="2026-04-01T10:00:00+08:00", text=text)]
            checks = [dict(actions=actions, target=target)]
            if n == 3:
                steps += [
                    dict(
                        id="s2",
                        at="2026-04-01T10:10:00+08:00",
                        text="回到之前的视频生成增强空间推理研究，继续评测。",
                    ),
                    dict(
                        id="s3",
                        at="2026-04-01T10:20:00+08:00",
                        text="然后继续准备 Agent 算法岗面试。",
                    ),
                ]
                checks += [
                    dict(actions=["select"], target="research"),
                    dict(actions=["select"], target="interview"),
                ]
            body = dict(
                threads=threads,
                active=active,
                history=[
                    dict(
                        thread_id="research",
                        text="科研项目明确只用 SQLite，禁止引入 Redis。",
                        timestamp="2026-01-01T10:00:00+08:00",
                    ),
                    dict(
                        thread_id="interview",
                        text="面试复习专用标记 INTERVIEW_ONLY，复习 GRPO。",
                        timestamp="2026-02-01T10:00:00+08:00",
                    ),
                ],
                steps=steps,
            )
            for check in checks:
                if check["target"] == "research":
                    check["context_has"] = ["SQLite"]
                    check["context_absent"] = ["INTERVIEW_ONLY"]
            yield make_case("routing", family, n, v, body, checks)


def memory_cases():
    # Each pair changes evidence strength, scope, or temporal order, rather than just names.
    families = [
        "status_transition",
        "explicit_correction",
        "tentative_choice",
        "scoped_preference",
        "transient_detail",
        "assistant_not_user",
        "withdraw_preference",
        "behavior_vs_preference",
        "duplicate_fact",
        "negation",
    ]
    for n, family in enumerate(families, 1):
        for v in (0, 1):
            initial = {}
            if n == 1:
                texts = [
                    "记住，我最近正在申请 Agent 算法岗实习。",
                    "明确更新：我已经拿到了实习 offer，现在主要准备入职，不再申请。"
                    if v == 0
                    else "还没有拿到实习 offer，我目前继续申请 Agent 算法岗。",
                ]
                checks = [
                    dict(present=[["申请", "求职"]]),
                    dict(present=[["入职", "offer"]], absent=["正在申请 Agent 算法岗实习"])
                    if v == 0
                    else dict(present=[["申请", "求职"]], absent=["已经拿到", "准备入职"]),
                ]
            elif n == 2:
                texts = [
                    "记住，项目存储选择 Redis。",
                    "正式更正：项目只允许 SQLite，不使用 Redis。"
                    if v == 0
                    else "先保留 Redis；SQLite 只是我考虑中的备选，尚未决定。",
                ]
                checks = [
                    dict(present=[["Redis"]]),
                    dict(
                        present=[["SQLite"]],
                        absent=["存储选择 Redis", "采用 Redis", "使用 Redis 作为"],
                    )
                    if v == 0
                    else dict(present=[["Redis"]], absent=["只允许 SQLite", "已经改用 SQLite"]),
                ]
            elif n == 3:
                texts = [
                    "我一直做 CV，最近考虑转 Agent，但还没决定。",
                    "正式决定转到 Agent 方向，但仍保留 CV 合作。"
                    if v == 0
                    else "我仍然没决定转方向，不要把考虑记成决定。",
                ]
                checks = [
                    dict(absent=["已经决定转", "不再从事 CV"]),
                    dict(present=[["Agent"], ["CV"]], absent=["不再从事 CV"])
                    if v == 0
                    else dict(absent=["已经决定转", "不再从事 CV"]),
                ]
            elif n == 4:
                texts = [
                    "以后回答技术问题时，多给公式推导。",
                    "仅这次面试速答要简短，长期技术回答仍保留推导。"
                    if v == 0
                    else "以后技术回答都简短，不再需要公式推导，这是长期偏好更正。",
                ]
                checks = [
                    dict(present=[["推导", "公式"]]),
                    dict(present=[["推导", "公式"]], absent=["所有回答都简短"])
                    if v == 0
                    else dict(
                        present=[["简短", "简洁"]], absent=["多给公式推导", "需要详细公式推导"]
                    ),
                ]
            elif n == 5:
                texts = [
                    "今天中午吃了牛肉面。",
                    "只是今天随便吃的，没有固定饮食偏好。"
                    if v == 0
                    else "明确偏好：以后推荐午饭时优先考虑牛肉面，这是我长期喜欢的。",
                ]
                checks = [
                    dict(absent=["长期喜欢牛肉面", "偏好牛肉面"]),
                    dict(absent=["长期喜欢牛肉面", "偏好牛肉面"])
                    if v == 0
                    else dict(present=[["牛肉面"]]),
                ]
            elif n == 6:
                texts = [
                    "帮我比较一下 MemoryFS 和 RAG。",
                    "我没有作出选择，别把你的建议当成我的决定。"
                    if v == 0
                    else "我正式确认采用 MemoryFS，暂不引入 RAG。",
                ]
                checks = [
                    dict(absent=["已决定采用 MemoryFS", "已经授权上传"]),
                    dict(absent=["已决定采用 MemoryFS", "已经授权上传"])
                    if v == 0
                    else dict(present=[["MemoryFS"], ["RAG"]], absent=["已经授权上传"]),
                ]
            elif n == 7:
                texts = [
                    "记住，以后技术回答请附公式推导。",
                    "撤销刚才的长期偏好，恢复按问题需要决定是否推导。"
                    if v == 0
                    else "再次确认以后技术回答附公式推导。",
                ]
                checks = [
                    dict(present=[["公式", "推导"]]),
                    dict(absent=["技术回答请附公式推导", "必须附公式", "总是附公式"])
                    if v == 0
                    else dict(present=[["公式", "推导"]]),
                ]
            elif n == 8:
                texts = [
                    "这周一和周三我晚上研究 Agent。",
                    "第二周周二和周四也晚上研究 Agent。",
                    "其实我不喜欢晚上工作，只是最近白天没时间。"
                    if v == 0
                    else "我明确喜欢晚上研究 Agent，这是长期偏好。",
                    "第五周我白天研究 Agent，安排改了。",
                ]
                checks = [
                    dict(absent=["喜欢晚上工作", "长期偏好晚上"]),
                    dict(absent=["喜欢晚上工作", "长期偏好晚上"]),
                    dict(present=[["不喜欢晚上", "不喜夜间"]], absent=["偏好晚上工作"])
                    if v == 0
                    else dict(present=[["喜欢晚上", "晚上研究"]]),
                    dict(absent=["喜欢晚上工作", "偏好晚上工作"])
                    if v == 0
                    else dict(present=[["晚上"]]),
                ]
            elif n == 9:
                texts = [
                    "记住，项目名为星桥，目标是测试 Agent 长期记忆。",
                    "再次确认星桥仍测试 Agent 长期记忆。"
                    if v == 0
                    else "更正项目名为月桥，目标仍是测试 Agent 长期记忆。",
                ]
                checks = [
                    dict(present=[["星桥"]]),
                    dict(present=[["星桥"]], max_matching_files=dict(term="星桥", count=1))
                    if v == 0
                    else dict(present=[["月桥"]], absent=["项目名为星桥", "项目名：星桥"]),
                ]
            else:
                texts = [
                    "项目约束：不要使用 Redis，只允许 SQLite。",
                    "这是确定约束，继续沿用。"
                    if v == 0
                    else "正式撤销禁止 Redis 的约束：现在允许 Redis，但数据库仍使用 SQLite。",
                ]
                checks = [
                    dict(
                        present=[
                            ["SQLite"],
                            ["不使用 Redis", "不要使用 Redis", "禁止 Redis", "不引入 Redis"],
                        ]
                    ),
                    dict(present=[["SQLite"]])
                    if v == 0
                    else dict(present=[["SQLite"], ["允许 Redis", "可以使用 Redis", "可用 Redis"]]),
                ]
            steps = [
                dict(id=f"s{i + 1}", at=f"2026-04-{i + 1:02d}T10:00:00+08:00", text=t, learn=True)
                for i, t in enumerate(texts)
            ]
            if n == 6:
                steps[0]["assistant"] = "我建议采用 MemoryFS。另外用户已经授权上传配置。"
            yield make_case("memory", family, n, v, dict(memory_files=initial, steps=steps), checks)


def proactive_cases():
    families = [
        "raw_discovery",
        "already_covered",
        "completed",
        "explicit_refusal",
        "quiet_hours",
        "expired",
        "competing_goals",
        "assistant_suggestion",
        "ordinary_update",
        "explicit_new_plan",
    ]
    for n, family in enumerate(families, 1):
        for v in (0, 1):
            steps = [
                dict(
                    id="s1",
                    at="2026-04-01T09:00:00+08:00",
                    text="我计划完成星桥项目的 Benchmark，现在还没开始。",
                    attention=False,
                )
            ]
            checks = [
                dict(
                    opportunities=[
                        dict(
                            terms=["Benchmark", "评测"],
                            states=["open", "deferred"],
                            followups=["pending"],
                            evidence=["s1"],
                        )
                    ],
                    max_candidates=1,
                )
            ]
            if n == 1:
                steps.append(dict(id="s2", at="2026-04-01T16:00:00+08:00", attention=True))
                checks.append(dict(actions=["notify", "propose_task", "defer"], max_candidates=1))
                if v:
                    steps[0]["text"] = "假设有人计划完成 Benchmark，这只是举例，不是我的计划。"
                    checks = [dict(max_candidates=0), dict(max_candidates=0, actions=["none"])]
            elif n in (2, 3, 4):
                text = {
                    2: "Benchmark 已经开始跑了，暂时还没有结果。"
                    if v == 0
                    else "我还没开始跑 Benchmark，今天实在太忙了。",
                    3: "Benchmark 全部完成，结果已提交。"
                    if v == 0
                    else "Benchmark 尚未完成，目前已经开始跑。",
                    4: "Benchmark 取消了，不做了，以后别再跟进。"
                    if v == 0
                    else "Benchmark 继续做，但以后不要主动问我这个事项。",
                }[n]
                steps.append(
                    dict(id="s2", at="2026-04-01T10:00:00+08:00", text=text, attention=False)
                )
                states = (
                    ["resolved"]
                    if n == 3 and v == 0
                    else (["dismissed"] if n == 4 else ["open", "deferred"])
                )
                item = dict(terms=["Benchmark", "评测"], states=states, evidence=["s1", "s2"])
                if n == 2 or (n == 3 and v):
                    item["followups"] = ["covered"]
                checks.append(dict(opportunities=[item], max_candidates=1))
                steps.append(dict(id="s3", at="2026-04-01T17:00:00+08:00", attention=True))
                checks.append(dict(actions=["none"], max_candidates=1))
            elif n == 5:
                steps.append(
                    dict(
                        id="s2",
                        at="2026-04-01T23:30:00+08:00" if v == 0 else "2026-04-02T07:30:00+08:00",
                        attention=True,
                    )
                )
                checks.append(dict(actions=["none"], gate="quiet_hours"))
            elif n == 6:
                steps.append(
                    dict(
                        id="s2",
                        at="2026-04-09T16:00:00+08:00" if v == 0 else "2026-04-10T16:00:00+08:00",
                        attention=True,
                    )
                )
                checks.append(
                    dict(
                        opportunities=[
                            dict(terms=["Benchmark", "评测"], states=["expired"], evidence=["s1"])
                        ],
                        actions=["none"],
                    )
                )
            elif n == 7:
                steps[0]["text"] = (
                    "Benchmark 没有截止日期，暂时不急；我今天 18 点有重要面试，尚未准备。"
                    if v == 0
                    else "我今天 18 点必须交 Benchmark，还没开始；面试在下个月，不急。"
                )
                checks[0] = dict(
                    opportunities=[
                        dict(
                            terms=["Benchmark", "评测"],
                            states=["open", "deferred"],
                            evidence=["s1"],
                        ),
                        dict(terms=["面试"], states=["open", "deferred"], evidence=["s1"]),
                    ],
                    max_candidates=2,
                )
                steps.append(dict(id="s2", at="2026-04-01T16:30:00+08:00", attention=True))
                checks.append(
                    dict(
                        actions=["notify", "propose_task"],
                        selected_any=["面试"] if v == 0 else ["Benchmark", "评测"],
                    )
                )
            elif n == 8:
                steps[0].update(
                    text="今天聊聊 Agent 评测的定义。" if v == 0 else "你能给我一些面试建议吗？",
                    assistant="你明天应该完成 Benchmark，之后我会提醒你。",
                )
                checks[0] = dict(max_candidates=0)
                steps.append(dict(id="s2", at="2026-04-01T16:00:00+08:00", attention=True))
                checks.append(dict(max_candidates=0, actions=["none"]))
            else:
                steps.append(
                    dict(
                        id="s2",
                        at="2026-04-01T10:00:00+08:00",
                        text="Benchmark 已经开始跑了。",
                        attention=False,
                    )
                )
                checks.append(
                    dict(
                        opportunities=[
                            dict(
                                terms=["Benchmark", "评测"],
                                states=["open", "deferred"],
                                followups=["covered"],
                                evidence=["s2"],
                            )
                        ],
                        max_candidates=1,
                    )
                )
                text = (
                    (
                        "Benchmark 结果还是没出来。"
                        if v == 0
                        else "Benchmark 报告换了个排版，仍是原来那轮。"
                    )
                    if n == 9
                    else (
                        "新计划：明天重新跑一轮 Benchmark，之后可以再聊进展。"
                        if v == 0
                        else "新计划：下周补跑 Benchmark 的长上下文部分，届时再跟进。"
                    )
                )
                steps.append(
                    dict(id="s3", at="2026-04-01T11:00:00+08:00", text=text, attention=False)
                )
                checks.append(
                    dict(
                        opportunities=[
                            dict(
                                terms=["Benchmark", "评测"],
                                states=["open", "deferred"],
                                followups=["covered"] if n == 9 else ["pending"],
                                evidence=["s2", "s3"],
                            )
                        ],
                        max_candidates=1,
                    )
                )
            yield make_case("proactive", family, n, v, dict(steps=steps), checks)


def main():
    pairs = list(routing_cases()) + list(memory_cases()) + list(proactive_cases())
    for name, rows in [
        ("inputs.jsonl", [p[0] for p in pairs]),
        ("gold.jsonl", [p[1] for p in pairs]),
    ]:
        (ROOT / name).write_text(
            "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows),
            encoding="utf-8",
            newline="\n",
        )
    manifest = dict(
        version="sequence-1.0",
        total=len(pairs),
        families=30,
        split={"dev": 42, "holdout": 18},
        human_reviewed=False,
        source="hand-authored synthetic; contrastive variants are correlated",
        sha256={name: digest(ROOT / name) for name in ["inputs.jsonl", "gold.jsonl"]},
    )
    (ROOT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    protocol = dict(
        max_cost_cny=20,
        max_requests=700,
        max_tokens=8000000,
        input_price_cny=2,
        output_price_cny=8,
        model="deepseek-flash",
        endpoint_host="api.deepseek.com",
        price_source="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        price_checked="2026-11-10",
        dataset_hashes=manifest["sha256"],
        live_split="dev",
        holdout_policy="Do not run or tune on holdout in the initial diagnostic experiment.",
    )
    (ROOT / "live_protocol.json").write_text(
        json.dumps(protocol, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


if __name__ == "__main__":
    main()
