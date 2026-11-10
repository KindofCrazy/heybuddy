"""Freeze 120 authored long sequences and the 360-case suite; no network or private data."""

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).parent
BASE = ROOT.parent
START = datetime.fromisoformat("2026-06-01T09:00:00+08:00")

PROJECTS = [
    ("视频空间推理", "生成视频", "相机轨迹", "三维评测"),
    ("论文复现", "随机种子", "消融实验", "复现实验"),
    ("语音助手", "唤醒词", "端点检测", "语音延迟"),
    ("笔记同步", "离线冲突", "增量同步", "同步协议"),
    ("机器人导航", "局部地图", "避障路径", "定位误差"),
    ("课程推荐", "先修关系", "课程负载", "选课规则"),
    ("文献管理", "引用格式", "重复条目", "论文标签"),
    ("图像去噪", "噪声水平", "图像指标", "训练数据"),
    ("日程助手", "时区转换", "重复事件", "冲突检查"),
    ("旅行规划", "交通衔接", "行李限制", "行程预算"),
    ("音乐检索", "旋律表示", "近邻检索", "曲库索引"),
    ("日志分析", "异常聚类", "事件排序", "日志采样"),
    ("食谱工具", "食材替代", "过敏标注", "份量换算"),
    ("校园地图", "无障碍路线", "楼层连接", "地图标注"),
    ("读书记录", "章节索引", "书摘整理", "阅读统计"),
    ("简历助手", "岗位匹配", "经历排序", "简历导出"),
    ("代码审查", "变更范围", "依赖检查", "静态规则"),
    ("数据标注", "标注指南", "分歧仲裁", "一致性检查"),
    ("问答评测", "答案归因", "证据完整性", "负例构造"),
    ("实验追踪", "运行配置", "指标对齐", "产物归档"),
]

MEMORY = [
    (
        "internship",
        "我正在申请算法实习",
        "我已经拿到 offer，正在准备入职",
        "我已经正式入职",
        ["申请"],
        ["offer", "入职"],
        ["正式入职"],
    ),
    (
        "project_stage",
        "星桥项目处于开发阶段",
        "星桥项目已经进入验收阶段",
        "星桥项目已经上线",
        ["开发"],
        ["验收"],
        ["上线"],
    ),
    (
        "database",
        "项目数据库确定使用 PostgreSQL",
        "项目数据库改为 SQLite，禁止引入 Redis",
        "项目仍用 SQLite，但现在允许 Redis 作缓存",
        ["PostgreSQL"],
        ["SQLite"],
        ["SQLite"],
    ),
    (
        "reply_style",
        "长期技术回答希望详细推导",
        "长期技术回答改为简短，按需补充推导",
        "长期技术回答恢复详细推导",
        ["推导"],
        ["简短", "简洁"],
        ["推导"],
    ),
    (
        "project_scope",
        "海报和博客原本都用橙色",
        "只把海报改为蓝色，博客仍用橙色",
        "海报改为绿色，博客仍用橙色",
        ["橙色"],
        ["蓝色"],
        ["绿色"],
    ),
    (
        "research_choice",
        "我主要从事 CV 研究",
        "我正式决定以 Agent 为主，同时保留 CV 合作",
        "我转回以 CV 为主，Agent 只做合作",
        ["CV"],
        ["Agent"],
        ["CV"],
    ),
    (
        "meeting_day",
        "长期组会安排在周二",
        "长期组会改到周四",
        "长期组会再次改到周五",
        ["周二"],
        ["周四"],
        ["周五"],
    ),
    (
        "home_city",
        "我的常住城市是南京",
        "我已经搬到杭州常住",
        "我已经搬到成都常住",
        ["南京"],
        ["杭州"],
        ["成都"],
    ),
    (
        "entity_scope",
        "我的导师姓王，合作者姓李",
        "合作者改为姓赵的同学，导师仍姓王",
        "合作者改为姓陈的同学，导师仍姓王",
        ["王"],
        ["赵"],
        ["陈"],
    ),
    (
        "deadline",
        "论文提交截止日期是六月十日",
        "论文截止日期正式延到六月二十日",
        "论文最终截止日期是六月二十五日",
        ["六月十日"],
        ["六月二十日"],
        ["六月二十五日"],
    ),
    (
        "rename",
        "长期项目代号是星桥",
        "项目正式更名为月桥",
        "项目最终更名为云桥",
        ["星桥"],
        ["月桥"],
        ["云桥"],
    ),
    (
        "diet",
        "我长期喜欢牛肉面",
        "午饭长期偏好改为米饭套餐",
        "午饭偏好改为清淡面食",
        ["牛肉面"],
        ["米饭"],
        ["清淡"],
    ),
    (
        "work_time",
        "最近晚上研究 Agent 只是白天忙，并非喜欢夜间工作",
        "我明确希望工作安排优先放白天",
        "我现在明确希望工作安排优先放上午",
        ["白天", "晚上"],
        ["白天"],
        ["上午"],
    ),
    (
        "relationship",
        "交流时请直接指出我的推理错误",
        "交流时先给结论，再指出推理错误",
        "交流时先指出关键错误，再给结论",
        ["错误"],
        ["结论"],
        ["错误"],
    ),
    (
        "assistant_claim",
        "项目配置内容不能上传外部服务",
        "配置仍不能上传，只允许我手动导出脱敏摘要",
        "仍禁止上传配置，取消脱敏摘要导出计划",
        ["不能上传", "禁止上传"],
        ["脱敏"],
        ["禁止上传", "不能上传"],
    ),
    (
        "withdraw",
        "回答默认需要给三个备选方案",
        "撤销三个备选方案要求，默认直接给建议",
        "以后默认给两个备选方案",
        ["三个", "三种"],
        ["直接"],
        ["两个", "两种"],
    ),
    (
        "duplicate",
        "评测项目名为长舟，目标是测长期记忆",
        "长舟的目标改为测任务恢复",
        "长舟的目标改为测约束遵循",
        ["长期记忆"],
        ["任务恢复"],
        ["约束"],
    ),
    (
        "language",
        "技术交流默认中文",
        "技术交流默认英文",
        "技术交流恢复默认中文",
        ["中文"],
        ["英文", "English"],
        ["中文"],
    ),
    (
        "storage_scope",
        "课程笔记和项目笔记都放本地",
        "只有课程笔记可以放网盘，项目笔记必须本地",
        "课程和项目笔记都必须本地保存",
        ["本地"],
        ["网盘"],
        ["本地"],
    ),
    (
        "conditional",
        "面试速答要简短，科研讨论要详细",
        "面试速答也要给公式，科研讨论仍详细",
        "面试速答恢复简短，科研讨论仍详细",
        ["简短"],
        ["公式"],
        ["简短"],
    ),
]

GOALS = [
    ("评测", "星桥评测", "云帆简历"),
    ("返修", "论文返修", "读书笔记"),
    ("发布", "代码发布", "课程整理"),
    ("作品", "作品集提交", "旅行草案"),
    ("报名", "会议报名", "书架整理"),
    ("报告", "实验报告", "照片归档"),
    ("面试", "算法面试", "音乐清单"),
    ("申请", "奖学金申请", "地图更新"),
    ("展示", "毕业展示", "食谱整理"),
    ("答辩", "项目答辩", "日志分析"),
    ("投稿", "论文投稿", "校园地图"),
    ("验收", "项目验收", "课程推荐"),
    ("搬迁", "搬家打包", "文献整理"),
    ("续约", "房租续约", "阅读统计"),
    ("预约", "证件预约", "旋律索引"),
    ("出行", "出差材料", "标注指南"),
    ("交付", "客户端交付", "噪声分析"),
    ("归档", "实验归档", "同步文档"),
    ("演讲", "会议演讲", "接口笔记"),
    ("开题", "科研开题", "书摘标签"),
]

DETAILS = [
    "先把评估目标和验收标准列清楚，不改变之前确认的安排。",
    "怎么区分实现难度和维护成本？这次先讨论方法。",
    "先分析小样本时指标波动的原因，不据此宣布最终结论。",
    "这一步请给一个反例，帮助我检查推理是否成立。",
    "先比较串行与批量处理的取舍，不替我作出新的选择。",
    "解释一下精确率和召回率的差异，我只是问概念。",
    "先讨论哪些信息必须保留原始证据，不新增计划。",
    "怎么记录版本差异才便于回溯？只讨论记录方式。",
    "先分析离线结果为何不一定代表真实用户体验。",
    "这一步把假设与已确认事实分开列出，保持原安排。",
    "解释一下时间预算和 Token 预算应该怎样区分。",
    "先检查评价标准有没有偏向某一种实现，不改变决定。",
    "这一步讨论边界情况，暂时不添加任何待办事项。",
    "如何设计对照实验来避免只挑有利样本？",
    "先说明哪些结论仍不确定，不把可能性记成事实。",
    "请解释为什么单次成功不能证明长期稳定性。",
    "先比较平均值和分组统计各能发现什么问题。",
    "这一步讨论失败分类，不改动任何已有项目状态。",
    "怎么避免摘要遗漏约束？先讨论一般原则。",
    "先复核解释是否有证据，暂时不增加新的长期偏好。",
    "这一轮只分析资源开销，不改变已确认的目标。",
    "说明哪些结果需要人工复核，暂不宣布新的结论。",
    "解释一下检索命中和答案正确为什么不是一回事。",
    "先讨论怎样保留未解决的问题，不把它当成新任务。",
    "这一步给出测试边界，保持已经确认的工作安排。",
    "如何识别重复证据？我是在问方法，不是增加计划。",
    "先解释为什么相关性不等于同一个任务。",
    "把模型建议和用户授权分开说明，不产生任何授权。",
    "这一轮只讨论如何报告失败，不改变已有事实。",
    "先说明结论适用的条件，不作额外推断。",
    "检查一下术语是否一致，不新增其他事项。",
    "解释长期记忆为什么需要记录时间范围。",
]


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def step(i, text, **options):
    return dict(
        id=f"u{i + 1:02d}", at=(START + timedelta(minutes=i * 3)).isoformat(), text=text, **options
    )


def record(track, family, number, variant, body, checks):
    cid = f"long-{track[0]}-{number + 1:02d}-{variant}"
    return dict(
        case_id=cid,
        track=track,
        family=family,
        split="dev" if number < 14 else "holdout",
        input=body,
    ), dict(case_id=cid, checks=checks)


def routes():
    for n, (project, detail1, detail2, detail3) in enumerate(PROJECTS):
        for v in (0, 1):
            length = 20 + 4 * (n % 4)
            tids = [
                str(uuid5(NAMESPACE_URL, f"hb-long-{n}-{role}"))
                for role in ("research", "interview", "personal")
            ]
            titles = [f"{project}研发", f"{project}面试", f"{project}出行"]
            states = [
                f"研究{detail1}与{detail2}，SQLite ONLY_RESEARCH，不引入 Redis。",
                f"准备{detail3}面试，INTERVIEW_ONLY。",
                "处理交通与住宿，TRAVEL_ONLY。",
            ]
            cards = [
                dict(
                    id=t,
                    title=title,
                    current_state=state,
                    last_active_at=f"2026-01-0{i + 1}T10:00:00+08:00",
                )
                for i, (t, title, state) in enumerate(zip(tids, titles, states, strict=True))
            ]
            distractors = 55 if n % 5 == 0 and v else 8
            cards += [
                dict(
                    id=str(uuid5(NAMESPACE_URL, f"hb-distractor-{n}-{j}")),
                    title=f"其他事项{j}",
                    current_state="与当前项目无关。",
                    last_active_at=f"2026-05-{1 + j // 24:02d}T{j % 24:02d}:00:00+08:00",
                )
                for j in range(distractors)
            ]
            history = [
                dict(
                    thread_id=t,
                    text=f"{title}专属历史：{state}",
                    timestamp=f"2026-01-0{i + 1}T10:00:00+08:00",
                )
                for i, (t, title, state) in enumerate(zip(tids, titles, states, strict=True))
            ]
            events = {
                0: (0, f"继续{titles[0]}，今天分析{detail1}。"),
                4: (1, f"切换回{titles[1]}，准备{detail3}。"),
                8: (0, f"回到{titles[0]}，继续分析{detail2}。"),
                12: (2, f"回到{titles[2]}，处理交通衔接。"),
                16: (0, f"再回到{titles[0]}，核对{detail3}。"),
                length - 1: (1 if v else 0, f"继续{titles[1 if v else 0]}，核对最后的约束。"),
            }
            active, current, steps, checks = tids[1], tids[1], [], []
            for i in range(length):
                action, target = ["continue"], current
                if i in events:
                    slot, text = events[i]
                    target = tids[slot]
                    action = ["continue"] if current == target else ["select"]
                    if i == 12 and n % 5 == 2:
                        text = f"新建一个独立的{project}第二项目，原项目保留，这次从零讨论新需求。"
                        action, target = ["new"], None
                        current = "@u13"
                    elif i == 12 and n % 5 == 3 and v == 0:
                        text = "之前那个项目再补充一点数据。"
                        action, target = ["clarify"], None
                    else:
                        current = target
                else:
                    slot = tids.index(current) if current in tids else None
                    title = titles[slot] if slot is not None else "刚才独立的新项目"
                    text = f"先继续{title}的当前步骤：" + DETAILS[(i + n + v) % len(DETAILS)]
                steps.append(step(i, text))
                expected = dict(actions=action, target=target)
                if target == tids[0]:
                    expected.update(
                        context_has=["ONLY_RESEARCH"],
                        context_absent=["INTERVIEW_ONLY", "TRAVEL_ONLY"],
                    )
                checks.append(
                    dict(
                        step=steps[-1]["id"],
                        kind="critical" if i in events else "continuity",
                        expected=expected,
                    )
                )
            yield record(
                "routing",
                f"routing_{n % 5}_{project}",
                n,
                v,
                dict(threads=cards, active=active, history=history, steps=steps),
                checks,
            )


def memories():
    for n, (family, a, b, c, aa, bb, cc) in enumerate(MEMORY):
        for v in (0, 1):
            length = 20 + 4 * (n % 4)
            final, final_alias = (a, aa) if v == 0 else (c, cc)
            events = {
                0: (f"记住，{a}。", aa),
                4: (
                    (
                        f"明确更新：{b}，替代之前的当前安排。"
                        if v == 0
                        else f"我在考虑是否改成：{b}。但尚未决定，目前仍是：{a}。"
                    ),
                    bb if v == 0 else aa,
                ),
                9: (f"现在正式确认：{b}。这不是假设，请更新当前状态。", bb),
                14: (f"再次明确更正：{final}。以本次表述为当前有效安排。", final_alias),
                length - 1: (
                    f"最终确认当前安排仍然是：{final}。不要因中间的讨论恢复旧状态。",
                    final_alias,
                ),
            }
            steps, checks = [], []
            for i in range(length):
                text = events[i][0] if i in events else DETAILS[(i + n + v) % len(DETAILS)]
                item = step(i, text, learn=i in events)
                if family == "assistant_claim" and i in (3, 8, 13):
                    item["assistant"] = "我声称用户已经授权上传项目配置；这只是助手生成的话。"
                steps.append(item)
                if i in events:
                    expected = dict(present=[events[i][1]])
                    if i in (9, 14, length - 1) or (i == 4 and v == 0):
                        current_fact = b if i in (4, 9) else final
                        expected["absent"] = [
                            old for old in (a, b) if old != current_fact and old not in current_fact
                        ]
                    if family == "assistant_claim":
                        expected.setdefault("absent", []).append("用户已经授权上传项目配置")
                    checks.append(
                        dict(
                            step=item["id"],
                            kind="final" if i == length - 1 else "critical",
                            expected=expected,
                        )
                    )
            yield record("memory", family, n, v, dict(memory_files={}, steps=steps), checks)


def opportunity(name, sources, *, status=("open", "deferred"), followup=None):
    value = dict(terms=[name], states=list(status), evidence=sources)
    if followup:
        value["followups"] = [followup]
    return value


def proactives():
    for n, (family, a, b) in enumerate(GOALS):
        for v in (0, 1):
            length = 20 + 4 * (n % 4)
            mode = n % 5
            events = {
                0: f"我计划完成{a}，现在还没开始。",
                4: f"另一个独立计划是{b}，目前也还没开始，但这个不急。",
                8: f"{a}已经开始做了，还没有完成。",
                12: (
                    f"{a}取消了，不做了，也不要再跟进。"
                    if v == 0
                    else f"{a}已经彻底做完，不需要继续关心。"
                ),
                16: (
                    f"新计划：重新做一轮{a}，这次需要今天十九点前完成，现在尚未开始，十八点可以聊进展。"
                    if v == 0
                    else f"{a}仍是之前已经结束的那件事，我只是在回顾旧记录，不要重开。"
                ),
            }
            if mode == 3:
                events[12] = f"{a}还没完成，但我已经告诉你正在做，今天不要再问进展。"
                events[16] = f"{a}仍在做，这是同一轮，不是新计划，也不用再问。"
                events[length - 1] = f"今天不要主动联系我询问{a}或{b}，这不是新计划。"
            if mode == 4:
                events[16] = (
                    f"新计划：{a}必须今天十九点前完成，尚未开始。{b}下个月再做，今天不要催。十八点可以聊紧急事项。"
                    if v == 0
                    else (
                        f"新计划：{b}必须今天十九点前完成，尚未开始。"
                        f"{a}保持之前结束的状态。十八点可以聊紧急事项。"
                    )
                )
            steps, checks = [], []
            closed = "dismissed" if v == 0 else "resolved"
            for i in range(length):
                text = events.get(i, DETAILS[(i + n + v) % len(DETAILS)])
                item = step(i, text, attention=False)
                steps.append(item)
                expected = None
                if i == 0:
                    expected = dict(
                        opportunities=[opportunity(a, ["u01"], followup="pending")],
                        max_candidates=1,
                    )
                elif i == 4:
                    expected = dict(
                        opportunities=[opportunity(a, ["u01", "u05"]), opportunity(b, ["u05"])],
                        max_candidates=2,
                    )
                elif i == 8:
                    expected = dict(
                        opportunities=[opportunity(a, ["u01", "u09"], followup="covered")],
                        max_candidates=2,
                    )
                elif i in (12, 16):
                    if mode == 3:
                        expected = dict(
                            opportunities=[
                                opportunity(
                                    a,
                                    ["u09", "u13", "u17"],
                                    status=["open", "deferred", "dismissed"],
                                )
                            ],
                            max_candidates=2,
                        )
                    elif i == 16 and v == 0:
                        expected = dict(
                            opportunities=[opportunity(a, ["u17"], followup="pending")],
                            max_candidates=2,
                        )
                    else:
                        expected = dict(
                            opportunities=[
                                opportunity(a, ["u01", "u09", "u13", "u17"], status=[closed])
                            ],
                            max_candidates=2,
                        )
                if expected:
                    for candidate in expected.get("opportunities", []):
                        candidate["evidence"] = [
                            s for s in candidate["evidence"] if int(s[1:]) <= i + 1
                        ]
                    checks.append(dict(step=item["id"], kind="critical", expected=expected))
            tick = dict(id="review", at="2026-06-01T18:00:00+08:00", attention=True)
            expected = dict(actions=["notify", "propose_task"], selected_any=[a] if v == 0 else [b])
            if mode == 1:
                tick["at"] = "2026-06-01T23:30:00+08:00"
                expected = dict(actions=["none"], gate="quiet_hours")
            elif mode == 2:
                tick["at"] = "2026-06-10T18:00:00+08:00"
                expected = dict(
                    actions=["none"],
                    opportunities=[
                        opportunity(
                            a,
                            ["u01", "u09", "u13", "u17"],
                            status=["expired"] if v == 0 else [closed],
                        )
                    ],
                )
            elif mode == 3:
                expected = dict(actions=["none", "silent", "defer"])
            elif mode == 0 and v == 1:
                # Only the undated secondary plan remains; no explicit current contact agreement.
                expected = dict(actions=["none", "silent", "defer"])
            expected["max_candidates"] = 2
            steps.append(tick)
            checks.append(dict(step="review", kind="final", expected=expected))
            yield record("proactive", f"proactive_{mode}_{family}", n, v, dict(steps=steps), checks)


def main():
    pairs = list(routes()) + list(memories()) + list(proactives())
    target = ROOT / "long"
    target.mkdir(parents=True, exist_ok=True)
    for name, values in [
        ("inputs.jsonl", [p[0] for p in pairs]),
        ("gold.jsonl", [p[1] for p in pairs]),
    ]:
        (target / name).write_text(
            "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in values),
            encoding="utf-8",
            newline="\n",
        )
    hashes = {name: sha(target / name) for name in ["inputs.jsonl", "gold.jsonl"]}
    dump(
        target / "manifest.json",
        dict(
            version="long-1.0",
            total=120,
            families=60,
            split=dict(dev=84, holdout=36),
            human_reviewed=False,
            sha256=hashes,
            user_messages=sum(
                sum(bool(s.get("text")) for s in c[0]["input"]["steps"]) for c in pairs
            ),
            checkpoints=sum(len(c[1]["checks"]) for c in pairs),
        ),
    )
    subsets = [
        ("primary", "v0_4", 150),
        ("challenge", "v0_4_challenge", 30),
        ("short", "sequence_v1", 60),
        ("long", "v1_0/long", 120),
    ]
    dump(
        ROOT / "manifest.json",
        dict(
            version="1.0.0",
            total=360,
            per_track=120,
            human_reviewed=False,
            subsets=[
                dict(
                    name=name,
                    path=path,
                    count=count,
                    sha256={
                        file: sha(BASE / path / file) for file in ["inputs.jsonl", "gold.jsonl"]
                    },
                )
                for name, path, count in subsets
            ],
            diagnostic_probes_excluded=4,
        ),
    )
    carry = json.loads((BASE / "sequence_v1/results/initial_dev.json").read_text(encoding="utf-8"))[
        "cumulative_usage"
    ]
    dump(
        ROOT / "live_protocol.json",
        dict(
            model="deepseek-flash",
            endpoint_host="api.deepseek.com",
            input_price_cny=2,
            output_price_cny=8,
            max_cost_cny=20,
            max_requests=4500,
            max_tokens=9000000,
            price_source="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
            carryover=carry,
            dataset_sha256=sha(ROOT / "manifest.json"),
            long_hashes=hashes,
            timeout_seconds=1200,
            stage_order=["smoke", "dev", "reserved", "short-reserved"],
            no_tuning_after_freeze=True,
            aggregation=(
                "one case one vote; latest final primary/challenge + frozen short dev "
                "+ newly evaluated short holdout + all long; missing/errors fail; "
                "diagnostics/smoke repeats excluded"
            ),
        ),
    )


if __name__ == "__main__":
    main()
