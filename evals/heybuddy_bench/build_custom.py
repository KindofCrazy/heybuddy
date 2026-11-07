#!/usr/bin/env python3
"""Deterministic world-state-first *candidate* cases for HeyBuddy-Bench.

This is a design seed, NOT a human-reviewed test set. Uses only stdlib.
Separates inputs from gold; no network, models, real user data, or generated LLM labels.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TZ = timezone(timedelta(hours=8))
START = datetime(2026, 9, 15, 10, 0, tzinfo=TZ)
NOW = datetime(2026, 11, 5, 16, 0, tzinfo=TZ)
RNG = random.Random(20261107)

# All people and organizations are fictional. Do not insert real user private history.
PROFILES = [
    dict(
        id="U01",
        name="林澄",
        role="软件开发者",
        a="私人助手应用",
        b="知识库检索服务",
        c="技术博客",
        d="周末徒步",
        keya="消息聚合",
        keyb="文档索引",
        keyc="文章排版",
        goal="上线个人助手测试版",
    ),
    dict(
        id="U02",
        name="周栩",
        role="产品设计师",
        a="客户门户改版",
        b="内部工单平台",
        c="作品集制作",
        d="慢跑",
        keya="移动端登录",
        keyb="后台权限",
        keyc="案例封面",
        goal="完成客户门户演示",
    ),
    dict(
        id="U03",
        name="陈宁",
        role="社区运营者",
        a="读书会活动",
        b="写作课程运营",
        c="小店采购",
        d="做饭",
        keya="报名海报",
        keyb="课后作业",
        keyc="补货明细",
        goal="提交读书会活动方案",
    ),
    dict(
        id="U04",
        name="许言",
        role="研究助理",
        a="传感器论文复现",
        b="实验室设备预约",
        c="研究生申请材料",
        d="爬山",
        keya="对照实验",
        keyb="仪器时段",
        keyc="推荐信",
        goal="完成实验报告",
    ),
    dict(
        id="U05",
        name="魏禾",
        role="独立开发者",
        a="手机记账应用",
        b="桌面时间管理器",
        c="发布说明写作",
        d="骑车",
        keya="账户同步",
        keyb="通知弹窗",
        keyc="更新日志",
        goal="发布记账应用内测",
    ),
    dict(
        id="U06",
        name="罗奕",
        role="餐饮店主",
        a="门店菜单改版",
        b="收银系统升级",
        c="新店招牌制作",
        d="看电影",
        keya="菜品定价",
        keyb="支付对账",
        keyc="门头字体",
        goal="确认新菜单",
    ),
    dict(
        id="U07",
        name="苏瑾",
        role="商业分析师",
        a="市场趋势报告",
        b="销售月度报告",
        c="现金流预算",
        d="游泳",
        keya="竞品样本",
        keyb="客户分层",
        keyc="成本预测",
        goal="交付市场趋势报告",
    ),
    dict(
        id="U08",
        name="顾宁",
        role="技术支持工程师",
        a="客服知识助手",
        b="工单分析报表",
        c="线上服务部署",
        d="打篮球",
        keya="问答召回",
        keyb="分类准确率",
        keyc="回滚脚本",
        goal="完成客服知识库升级",
    ),
    dict(
        id="U09",
        name="沈宁",
        role="摄影师",
        a="个人摄影展",
        b="摄影培训课程",
        c="客户相册精修",
        d="逛公园",
        keya="展厅灯光",
        keyb="讲义示例",
        keyc="肤色校准",
        goal="提交摄影展布展计划",
    ),
    dict(
        id="U10",
        name="唐晓",
        role="机器人学生",
        a="视觉模型训练",
        b="移动机器人导航",
        c="毕业论文写作",
        d="烘焙",
        keya="数据增强",
        keyb="路径代价",
        keyc="实验表格",
        goal="完成视觉模型验证",
    ),
]


def when(days: int = 0, hours: int = 0, base=START) -> str:
    return (base + timedelta(days=days, hours=hours)).isoformat()


def add_case(cases, golds, *, cid, track, category, user, input_, gold, truth, group):
    cases.append(
        {
            "case_id": cid,
            "track": track,
            "source": {"type": "custom", "method": "controlled_template"},
            "scenario_group": group,
            "user_id": user["id"],
            "category": category,
            "split": "candidate_test",
            "review_status": "pending_human_review",
            "input": input_,
        }
    )
    golds.append(
        {
            "case_id": cid,
            "track": track,
            "gold": gold,
            "world_state": truth,
            "review_status": "pending_human_review",
        }
    )


def build_routing(cases, golds):
    for i in range(40):
        p = PROFILES[i % 10]

        def tid(k, profile=p):
            return f"{profile['id']}_{k}"

        # Thread metadata is the only stable identity; no gold topic IDs in model input.
        threads = [
            {
                "id": tid("A"),
                "title": p["a"],
                "current_state": f"正在处理{p['keya']}，最近一次完成了检查清单",
                "last_active_at": when(5),
            },
            {
                "id": tid("B"),
                "title": p["b"],
                "current_state": f"正在处理{p['keyb']}，最近完成了初稿",
                "last_active_at": when(25),
            },
            {
                "id": tid("C"),
                "title": p["c"],
                "current_state": f"正在处理{p['keyc']}，等反馈",
                "last_active_at": when(19),
            },
        ]
        prev = {
            "history": [
                {
                    "episode_id": tid("E1"),
                    "timestamp": when(3),
                    "thread_id": tid("A"),
                    "messages": [
                        f"我们先做{p['a']}的{p['keya']}。",
                        f"把{p['keya']}的问题列成清单。",
                    ],
                },
                {
                    "episode_id": tid("E2"),
                    "timestamp": when(24),
                    "thread_id": tid("B"),
                    "messages": [f"现在要推进{p['b']}的{p['keyb']}。"],
                },
                {
                    "episode_id": tid("E3"),
                    "timestamp": when(18),
                    "thread_id": tid("C"),
                    "messages": [f"{p['c']}的{p['keyc']}我已经发给同事看了。"],
                },
            ],
            "thread_candidates": threads,
            "now": when(42),
        }
        if i < 10:  # old thread return, active is another
            active = tid("B")
            msg = f"我们回到之前的{p['a']}吧，{p['keya']}那份检查清单还差最后一步。"
            act = "select"
            target = tid("A")
            cat = "long_gap_resume"
            evidence = [tid("E1")]
        elif i < 20:  # overlapping domains, unique target
            active = tid("A")
            msg = f"{p['b']}上次说的{p['keyb']}，现在按另一份方案改；别动{p['a']}。"
            act = "select"
            target = tid("B")
            cat = "similar_thread_disambiguation"
            evidence = [tid("E2")]
        elif i < 28:
            active = tid("A")
            if i < 24:
                msg = f"{p['a']}的{p['keya']}再加一个回归用例，还是刚才那部分。"
                act = "continue"
                target = tid("A")
                cat = "subtask_existing"
                evidence = [tid("E1")]
            else:
                new_titles = ["家庭共享日历", "宠物照料记录工具", "社区置换活动", "个人收支可视化"]
                new_title = new_titles[i - 24]
                msg = f"另外我准备开启一个全新的{new_title}，先讨论需求。这个和{p['a']}无关。"
                act = "new"
                target = None
                cat = "distinct_new_task"
                evidence = []
        elif i < 34:
            active = tid("B")
            msg = [
                "早安！",
                "今天想随便聊聊音乐。",
                "晚安。",
                "今天外面天气真舒服。",
                "你好呀。",
                "周末我想放松一下。",
            ][(i - 28)]
            act = "chat"
            target = None
            cat = "ephemeral_social"
            evidence = []
        else:
            active = None
            # Two plausible referents and no resolving recent history.
            msg = [
                "之前那份清单的格式再调一下。",
                "上次讨论的文档可以继续吗？",
                "把之前那个版本回退。",
                "那个评审意见还有吗？",
                "我们之前说的性能问题是什么来着？",
                "把那份初稿继续写完吧。",
            ][(i - 34)]
            # replace thread states for ambiguity, both carry same unqualified noun
            threads[0]["current_state"] = "之前提交了一份清单、初稿和评审反馈"
            threads[1]["current_state"] = "之前也提交了一份清单、初稿和评审反馈"
            act = "clarify"
            target = None
            cat = "ambiguous_reference"
            evidence = []
        inp = dict(
            prev,
            active_thread_id=active,
            current_message=msg,
            recent_context=[]
            if active is None
            else [
                {
                    "timestamp": when(35),
                    "thread_id": active,
                    "text": f"刚才聊了{p['b'] if active == tid('B') else p['a']}。",
                }
            ],
        )
        gold = {"action": act, "thread_id": target, "acceptable_actions": [act]}
        truth = {
            "rationale": cat,
            "correct_evidence_episode_ids": evidence,
            "thread_identity": tid("A") if act == "select" and i < 10 else target,
        }
        add_case(
            cases,
            golds,
            cid=f"custom_rt_{i + 1:03d}",
            track="thread_routing",
            category=cat,
            user=p,
            input_=inp,
            gold=gold,
            truth=truth,
            group=f"{p['id']}_routing",
        )


def fact(key, value, eids):
    return {"key": key, "value": value, "evidence_ids": eids}


def build_memory(cases, golds):
    for i in range(30):
        p = PROFILES[i % 10]
        n = i + 1
        eid = f"M{n:03d}_E1"
        eid2 = f"M{n:03d}_E2"
        t = f"2026-10-{(i % 20) + 3:02d}T20:00:00+08:00"
        memory_files = {
            "human.md": "# User\n- 喜欢使用 Python 编写脚本。",
            f"topics/{p['id'].lower()}-project.md": f"# {p['a']}\n- 当前正在处理{p['keya']}。",
        }
        category = ""
        events = []
        action = ""
        target = None
        old = ""
        new = ""
        facts = []
        why = ""
        if i < 4:  # create topic memory
            category = "new_fact"
            action = "create"
            target = f"topics/{p['id'].lower()}-reading.md"
            events = [
                {
                    "id": eid,
                    "timestamp": t,
                    "speaker": "user",
                    "text": f"我开始长期整理「{p['c']}」这件事，接下来几周都会跟进。",
                }
            ]
            new = f"# {p['c']}\n- 当前长期推进{p['c']}。"
            facts = [fact(f"project.{p['c']}.status", "ongoing", [eid])]
        elif i < 8:  # project update
            category = "explicit_project_update"
            action = "update"
            target = f"topics/{p['id'].lower()}-project.md"
            old = memory_files[target]
            new = f"# {p['a']}\n- {p['keya']}已完成，现在转入验收阶段。"
            events = [
                {
                    "id": eid,
                    "timestamp": t,
                    "speaker": "user",
                    "text": f"确认一下：{p['a']}的{p['keya']}已经做完，下一步改成验收。",
                }
            ]
            facts = [fact(f"project.{p['a']}.stage", "acceptance", [eid])]
        elif i < 10:
            category = "explicit_preference_override"
            action = "update"
            target = "human.md"
            old = memory_files["human.md"]
            new = "# User\n- 目前主要使用 TypeScript 编写脚本；以前使用 Python。"
            events = [
                {
                    "id": eid,
                    "timestamp": t,
                    "speaker": "user",
                    "text": (
                        "我现在脚本基本都用 TypeScript 了，Python 是以前的习惯，"
                        "以后按现在这个偏好记。"
                    ),
                }
            ]
            facts = [fact("user.scripting_preference", "TypeScript", [eid])]
        elif i < 15:
            category = "core_vs_deferred"
            if i < 13:
                action = "update"
                target = "human.md"
                old = memory_files[target]
                new = old + "\n- 希望默认用简短中文回复。"
                events = [
                    {
                        "id": eid,
                        "timestamp": t,
                        "speaker": "user",
                        "text": "以后跟我聊天请默认用简短中文回复，这个是长期习惯。",
                    }
                ]
                facts = [fact("user.response_style", "concise_chinese", [eid])]
            else:
                action = "create"
                target = f"topics/{p['id'].lower()}-new-plan.md"
                new = f"# {p['goal']}\n- 用户计划推进该目标。"
                events = [
                    {
                        "id": eid,
                        "timestamp": t,
                        "speaker": "user",
                        "text": f"我想把「{p['goal']}」作为这个月的项目目标。",
                    }
                ]
                facts = [fact(f"project.{p['goal']}.status", "planned", [eid])]
        elif i < 20:
            category = "behavioral_regularity"
            if i < 18:
                action = "create"
                target = f"topics/{p['id'].lower()}-work-pattern.md"
                new = "# 工作习惯\n- 用户通常在晚饭后安排一小时处理个人项目。"
                events = [
                    {
                        "id": eid,
                        "timestamp": when(2),
                        "speaker": "user",
                        "text": "这周一晚饭后我安排了一小时做个人项目。",
                    },
                    {
                        "id": eid2,
                        "timestamp": when(9),
                        "speaker": "user",
                        "text": "上周也是晚饭后固定做一小时项目。",
                    },
                    {
                        "id": f"M{n:03d}_E3",
                        "timestamp": t,
                        "speaker": "user",
                        "text": (
                            "这已经是我连续第三周这样安排了，"
                            "一般晚饭后那一个小时是我的固定项目时间。"
                        ),
                    },
                ]
                facts = [
                    fact(
                        "user.work_pattern",
                        "personal_projects_after_dinner",
                        [eid, eid2, f"M{n:03d}_E3"],
                    )
                ]
            else:
                action = "reject"
                target = None
                events = [
                    {
                        "id": eid,
                        "timestamp": t,
                        "speaker": "user",
                        "text": "昨天晚饭后临时写了一小时代码，以前没有这样的习惯。",
                    }
                ]
                why = "One-off observation is insufficient to infer a recurring behavior."
        elif i < 25:
            category = "temporal_provenance"
            if i < 22:
                action = "update"
                target = f"topics/{p['id'].lower()}-project.md"
                old = memory_files[target]
                new = f"# {p['a']}\n- 截至本周，{p['keya']}因新安排暂缓，后续待确认。"
                events = [
                    {
                        "id": eid,
                        "timestamp": when(2),
                        "speaker": "user",
                        "text": f"{p['a']}的{p['keya']}开始做了。",
                    },
                    {
                        "id": eid2,
                        "timestamp": t,
                        "speaker": "user",
                        "text": f"刚更新：{p['a']}的{p['keya']}这周暂缓，之后再看。",
                    },
                ]
                facts = [fact(f"project.{p['a']}.status", "paused", [eid2])]
            elif i < 24:
                action = "no_op"
                target = None
                events = [
                    {
                        "id": eid,
                        "timestamp": t,
                        "speaker": "user",
                        "text": (
                            f"这里是假设讨论：如果{p['a']}的{p['keya']}完成了，"
                            "会有什么风险？现实里还没完成。"
                        ),
                    }
                ]
                why = "Hypothetical completion must not supersede actual status."
            else:
                action = "review"
                target = None
                events = [
                    {
                        "id": eid,
                        "timestamp": t,
                        "speaker": "user",
                        "text": (
                            f"另一位同事说{p['a']}的进展已经全变了，"
                            "但我还没核实，也不清楚具体变成什么。"
                        ),
                    }
                ]
                why = "Conflicting second-hand source, unspecified new state."
        else:
            category = "ignore_or_archive"
            if i < 28:
                action = "reject"
                target = None
                events = [
                    {
                        "id": eid,
                        "timestamp": t,
                        "speaker": "user",
                        "text": f"今天午餐临时选了面条，和{p['a']}没有关系，不需要长期记住。",
                    }
                ]
                why = "Explicit ephemeral, not a durable memory."
            else:
                action = "update"
                target = f"topics/{p['id'].lower()}-project.md"
                old = memory_files[target]
                new = f"# {p['a']}\n- {p['keya']}已取消；历史资料保留，不再视为进行中。"
                events = [
                    {
                        "id": eid,
                        "timestamp": t,
                        "speaker": "user",
                        "text": f"{p['a']}的{p['keya']}确定取消了，别再按正在做的计划推进。",
                    }
                ]
                facts = [fact(f"project.{p['a']}.status", "cancelled", [eid])]
        if action == "create" and target in memory_files:
            raise AssertionError("Create target exists")
        if action == "update" and target not in memory_files:
            raise AssertionError("Update target missing")
        inp = {
            "now": t,
            "user_id": p["id"],
            "memory_files": memory_files,
            "episode": {"id": f"M{n:03d}_EP", "thread_id": f"{p['id']}_A", "events": events},
            "memory_catalogue": list(memory_files),
            "task": "Propose one memory learning decision for this episode.",
        }
        gold = {
            "action": action,
            "target_path": target,
            "old_text": old if action == "update" else "",
            "new_text": new if action in ("create", "update") else "",
            "facts": facts,
            "evidence_ids": sorted({e for f in facts for e in f["evidence_ids"]}),
        }
        truth = {
            "reason": why or category,
            "world_state_facts": facts,
            "evidence_user_only": True,
            "file_exists": target in memory_files if target else None,
        }
        add_case(
            cases,
            golds,
            cid=f"custom_ml_{n:03d}",
            track="memory_learning",
            category=category,
            user=p,
            input_=inp,
            gold=gold,
            truth=truth,
            group=f"{p['id']}_memory",
        )


def build_proactive(cases, golds):
    for i in range(35):
        p = PROFILES[i % 10]
        n = i + 1
        pid = f"P{n:03d}"
        now = NOW + timedelta(days=i // 7)
        if 10 <= i < 13:
            now = NOW  # minimal pairs: P001–P003 with P011–P013 (completion event)
        if 18 <= i < 23:
            now = now.replace(hour=20)  # recurring after-dinner check-in
        now_s = now.isoformat()
        due = (now + timedelta(hours=5)).isoformat()
        ev = f"{pid}_E1"
        ev2 = f"{pid}_E2"
        evidence = [
            {
                "id": ev,
                "timestamp": (now - timedelta(days=4)).isoformat(),
                "text": (
                    f"我计划在{now.date().isoformat()}晚上21:00前完成「{p['goal']}」，"
                    "临近时可以提醒我检查进度。"
                ),
            }
        ]
        cand = {
            "id": pid,
            "title": p["goal"],
            "topic_key": f"{p['id'].lower()}_goal",
            "status": "open",
            "context": f"用户承诺完成{p['goal']}；尚无完成记录。",
            "due_at": due,
            "source_evidence_ids": [ev],
            "last_notified_at": None,
            "followup_state": "pending",
        }
        state = {
            "availability": "free",
            "quiet_hours": ["23:00", "08:00"],
            "user_local_timezone": "Asia/Shanghai",
            "explicit_do_not_disturb": False,
            "last_interaction_at": (now - timedelta(hours=8)).isoformat(),
        }
        action = ""
        cat = ""
        next_review = None
        rationale = ""
        new_evidence = []
        if i < 10:
            cat = "deadline_commitment"
            if i < 3:
                action = "notify"
                rationale = "Due soon, user asked for check-in, free now."
            elif i < 6:
                action = "defer"
                next_review = (now + timedelta(hours=2)).isoformat()
                state["availability"] = "focus"
                rationale = "Due soon but focus session ongoing."
            elif i < 8:
                action = "silent"
                cand["due_at"] = (now + timedelta(days=7)).isoformat()
                evidence[0]["text"] = (
                    f"我计划在{(now + timedelta(days=7)).date().isoformat()}前完成"
                    f"「{p['goal']}」，临近时再提醒。"
                )
                rationale = "No timely reason to remind a week early."
            else:
                action = "propose_task"
                cand["title"] = "代用户提交外部表单"
                cand["context"] = "用户想让助手代为提交外部表单，但尚未明确授权。"
                cand["due_at"] = None
                evidence = [
                    {
                        "id": ev,
                        "timestamp": (now - timedelta(days=1)).isoformat(),
                        "text": "我希望你能替我提交申请表，但目前还没授权你操作账户。",
                    }
                ]
                rationale = "Requires user authorization before external operation."
        elif i < 18:
            cat = "completed_or_cancelled"
            if i < 14:
                action = "silent"
                cand["status"] = "resolved"
                cand["context"] = f"用户已确认{p['goal']}完成。"
                new_evidence = [
                    {
                        "id": ev2,
                        "timestamp": (now - timedelta(hours=1)).isoformat(),
                        "text": f"{p['goal']}我已经完成了，不用再提醒。",
                    }
                ]
                rationale = "Completed item must not generate follow-up."
            elif i < 16:
                action = "silent"
                cand["status"] = "dismissed"
                cand["context"] = f"用户明确取消了{p['goal']}。"
                new_evidence = [
                    {
                        "id": ev2,
                        "timestamp": (now - timedelta(hours=1)).isoformat(),
                        "text": f"{p['goal']}取消了，后面都不做。",
                    }
                ]
                rationale = "Cancelled item must not generate follow-up."
            else:
                action = "notify"
                cand["status"] = "open"
                cand["context"] = f"用户重新启动{p['goal']}并要求本周提醒。"
                new_evidence = [
                    {
                        "id": ev2,
                        "timestamp": (now - timedelta(hours=1)).isoformat(),
                        "text": f"我决定重新启动{p['goal']}，今天下午再提醒我一下。",
                    }
                ]
                rationale = "Explicit reopening and a new reminder instruction."
        elif i < 23:
            cat = "behavioral_evidence"
            cand["title"] = "晚饭后个人项目复盘"
            cand["due_at"] = None
            evidence = [
                {
                    "id": ev,
                    "timestamp": (now - timedelta(days=14)).isoformat(),
                    "text": "上次我晚饭后整理了一小时项目。",
                }
            ]
            if i < 20:
                action = "notify"
                new_evidence = [
                    {
                        "id": ev2,
                        "timestamp": (now - timedelta(days=7)).isoformat(),
                        "text": "连续三周我都晚饭后固定复盘；以后这个时段请问问我是不是要开始。",
                    }
                ]
                cand["context"] = "用户有明确、重复的每周复盘安排，且请求在当前时段跟进。"
                rationale = "Explicit recurring preference, supported by multiple events."
            elif i < 22:
                action = "silent"
                cand["context"] = "只有一次偶然的晚间活动，没有提醒承诺。"
                rationale = "One-off behavior cannot justify unsolicited reminder."
            else:
                action = "defer"
                next_review = (now + timedelta(hours=3)).isoformat()
                state["availability"] = "focus"
                new_evidence = [
                    {
                        "id": ev2,
                        "timestamp": (now - timedelta(days=7)).isoformat(),
                        "text": "每周晚饭后会复盘，但专注时别打扰。",
                    }
                ]
                cand["context"] = "有重复习惯，但当前是专注时段。"
                rationale = "Recurring cue exists but current time is inappropriate."
        elif i < 30:
            cat = "attention_interruption"
            if i < 27:
                action = "defer"
                next_review = (now + timedelta(hours=2)).isoformat()
                state["availability"] = "focus"
                rationale = "Do not interrupt ongoing focus."
            elif i < 29:
                action = "silent"
                state["explicit_do_not_disturb"] = True
                new_evidence = [
                    {
                        "id": ev2,
                        "timestamp": (now - timedelta(minutes=5)).isoformat(),
                        "text": "今天不要再主动提醒我，明天再说。",
                    }
                ]
                rationale = "Explicit do-not-disturb takes precedence."
            else:
                action = "notify"
                state["availability"] = "free"
                new_evidence = [
                    {
                        "id": ev2,
                        "timestamp": (now - timedelta(hours=2)).isoformat(),
                        "text": "我今天下午空着，你可以提醒我检查进度。",
                    }
                ]
                rationale = "New permission and user availability."
        else:
            cat = "repeat_cooldown"
            if i < 34:
                action = "silent"
                cand["last_notified_at"] = (now - timedelta(minutes=12)).isoformat()
                rationale = "Recently notified, no new actionable information."
            else:
                action = "notify"
                cand["last_notified_at"] = (now - timedelta(days=3)).isoformat()
                new_evidence = [
                    {
                        "id": ev2,
                        "timestamp": (now - timedelta(hours=2)).isoformat(),
                        "text": f"{p['goal']}出现了新阻碍，今天请再问一次进度。",
                    }
                ]
                rationale = "New state and renewed explicit request after cooldown."
        evi = evidence + new_evidence
        cand["source_evidence_ids"] = [x["id"] for x in evi]
        inp = {
            "now": now_s,
            "user_id": p["id"],
            "attention_context": state,
            "recent_user_evidence": evi,
            "candidates": [cand],
            "task": "Choose a single attention decision. Do not actually send anything.",
        }
        gold = {
            "action": action,
            "proposal_ids": [pid],
            "next_review_at": next_review,
            "evidence_ids": [x["id"] for x in evi],
            "trigger_window": {"start": now_s, "end": (now + timedelta(hours=2)).isoformat()}
            if action == "notify"
            else None,
        }
        truth = {
            "reason": rationale,
            "user_task_state": cand["status"],
            "user_busy": state["availability"] == "focus",
            "has_dnd": state["explicit_do_not_disturb"],
            "expected_action": action,
        }
        add_case(
            cases,
            golds,
            cid=f"custom_pd_{n:03d}",
            track="proactive_decision",
            category=cat,
            user=p,
            input_=inp,
            gold=gold,
            truth=truth,
            group=f"{p['id']}_proactive",
        )


def write_jsonl(path, items):
    with path.open("w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")


def main():
    cases = []
    golds = []
    build_routing(cases, golds)
    build_memory(cases, golds)
    build_proactive(cases, golds)
    assert len(cases) == len(golds) == 105
    assert len({x["case_id"] for x in cases}) == 105
    ct = Counter(x["track"] for x in cases)
    assert ct == {"thread_routing": 40, "memory_learning": 30, "proactive_decision": 35}
    write_jsonl(ROOT / "custom_inputs.jsonl", cases)
    write_jsonl(ROOT / "custom_gold.jsonl", golds)
    pairs = [
        {
            "positive": f"custom_pd_{j:03d}",
            "negative": f"custom_pd_{j + 10:03d}",
            "difference": "Task completion evidence + resolved status",
        }
        for j in (1, 2, 3)
    ]
    (ROOT / "contrastive_pairs.json").write_text(
        json.dumps(pairs, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (ROOT / "synthetic_personas.json").open("w", encoding="utf-8") as f:
        json.dump(PROFILES, f, ensure_ascii=False, indent=2)
    hashes = {
        f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest()
        for f in (
            "custom_inputs.jsonl",
            "custom_gold.jsonl",
            "synthetic_personas.json",
            "contrastive_pairs.json",
        )
    }
    manifest = {
        "version": "0.1.0-seed",
        "data_status": "candidate_unreviewed",
        "seed": 20261107,
        "counts": dict(ct),
        "total": len(cases),
        "human_reviewed": False,
        "generator": "world_state_first_controlled_templates",
        "sha256": hashes,
        "source_project": "https://github.com/KindofCrazy/heybuddy",
        "notes": "Historical custom seed only; see the primary manifest for the assembled dataset.",
    }
    (ROOT / "seed_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("generated", dict(ct), "total", len(cases))


if __name__ == "__main__":
    main()
