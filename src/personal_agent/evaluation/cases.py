"""Frozen ground truth prepared before Phase 2 prompt edits. No private fixtures."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    user: str
    expected: dict
    split: str = "dev"
    topic: str = ""
    path: str = "projects/heybuddy.md"
    history: tuple[tuple[str, str], ...] = ()
    kind: str = "normal"
    automation: bool = True


def topic(body: str, name: str = "heybuddy") -> str:
    return f"---\nname: {name}\ndescription: {name} 项目架构与决策。\n---\n\n# {name}\n\n{body}\n"


BASE_TOPIC = topic(
    "采用自建轻量 asyncio Agent Harness，不引入 LangGraph。"
    "会话持久化使用 SQLite。\n\n架构校验标记 KEEP-88。"
)
CASES = (
    Case(
        "direct_python",
        "Direct Answer",
        "Python 的 tuple 是否可变？请一句话明确回答「不可变」。",
        {"answer_any": ["不可变", "immutable"], "max_tools": 0, "memory": "negative"},
    ),
    Case(
        "deferred_project",
        "Deferred Retrieval",
        "请读取 HeyBuddy 已有项目记忆，说明它的 Harness 与持久化架构。",
        {"read": "projects/heybuddy.md", "answer_all": ["asyncio", "SQLite"], "memory": "negative"},
        topic=BASE_TOPIC,
    ),
    Case(
        "recall_discussion",
        "Recall",
        "我们以前讨论的发布代号是什么？请查询原始历史。",
        {"tool": "recall_search", "answer_all": ["ORBIT-17"]},
        history=(("正式决定发布代号为 ORBIT-17。", "已确认发布代号。"),),
        automation=False,
    ),
    Case(
        "memory_missing",
        "Memory Missing",
        "我的私人项目代号是什么？若未记录，请回答「无记录」，不要猜。",
        {"answer_any": ["无记录"], "memory": "negative"},
    ),
    Case(
        "stable_preference",
        "Stable Preference",
        "以后解释代码，我希望先理解为什么这样设计，再看实现细节。这是我的长期稳定偏好。",
        {
            "memory": "update",
            "target": "human.md",
            "after_any": ["为什么", "理由", "why", "rationale"],
        },
    ),
    Case(
        "project_decision",
        "Project Decision",
        "我们决定暂停 Background Task Runtime，下一阶段先完善 Memory Automation。"
        "HeyBuddy 的其他架构保持不变。",
        {
            "memory": "update",
            "target": "projects/heybuddy.md",
            "after_all": ["Memory Automation", "KEEP-88"],
            "removed": "下一阶段实现 Background Task Runtime。",
        },
        topic=BASE_TOPIC + "\n下一阶段实现 Background Task Runtime。\n",
    ),
    Case(
        "correction",
        "Correction",
        "HeyBuddy 之前采用 Redis 的缓存方案已废弃，现在正式改为 SQLite 的 cache_entries 表。",
        {
            "memory": "update",
            "target": "projects/heybuddy.md",
            "after_all": ["cache_entries", "KEEP-88"],
            "removed": "缓存层采用 Redis。",
        },
        topic=BASE_TOPIC + "\n缓存层采用 Redis。\n",
    ),
    Case(
        "hypothesis",
        "Temporary Hypothesis",
        "我正在考虑使用 Redis，还没决定。这只是假设，请分析取舍，别当成已采用的方案。",
        {"memory": "negative", "max_tools": 0},
    ),
    Case(
        "duplicate",
        "Duplicate Memory",
        "重申我们既有的 HeyBuddy 决策：采用自建轻量 asyncio Agent Harness，"
        "不引入 LangGraph，会话持久化继续使用 SQLite。",
        {"memory": "no_op"},
        topic=BASE_TOPIC,
    ),
    Case(
        "tool_failure",
        "Tool Failure",
        "请查询演示服务状态。若查询工具失败，直接说明失败，不要重试或推测成功。",
        {
            "tool": "demo_status",
            "exact_tool_calls": 1,
            "answer_any": ["失败", "不可用", "unavailable"],
            "memory": "negative",
        },
        kind="failure",
    ),
    Case(
        "long_context",
        "Long Context",
        "当前最终方案和 release-check 下一步是什么？请区分已成功和失败的操作。",
        {
            "summary_all": ["release-check"],
            "summary_any": ["方案 B", "方案B", "plan B"],
            "summary_failure": ["失败", "未成功", "failed", "not successful"],
        },
        kind="compaction",
        automation=False,
    ),
    Case(
        "cross_session",
        "Cross-session",
        "在这个新会话中，请读取已有 HeyBuddy 项目记忆，部署标记是什么？",
        {"read": "projects/heybuddy.md", "answer_all": ["SHIP-29"], "memory": "negative"},
        topic=BASE_TOPIC + "\n当前部署标记为 SHIP-29。\n",
        kind="cross_session",
    ),
    Case(
        "direct_dict",
        "Direct Answer",
        "Python dict 可以同时存在两个相等的键吗？请明确回答「不能」。",
        {"answer_any": ["不能"], "max_tools": 0, "memory": "negative"},
        split="holdout",
    ),
    Case(
        "fictional_location",
        "Temporary Hypothesis",
        "假设我搬到上海，通勤方式如何比较？这只是虚构示例，不代表我的所在地或真实计划。",
        {"memory": "negative", "max_tools": 0},
        split="holdout",
    ),
    Case(
        "preference_repro",
        "Stable Preference",
        "以后给我调试建议，请先提供最小复现步骤，再解释修复理由。这是我确认的长期偏好。",
        {"memory": "update", "target": "human.md", "after_any": ["复现", "repro"]},
        split="holdout",
    ),
    Case(
        "correction_docs",
        "Correction",
        "Falcon 文档项目已决定弃用 AlphaDocs，正式改用 MkDocs。请保留其他项目知识。",
        {
            "memory": "update",
            "target": "projects/falcon.md",
            "after_all": ["MkDocs", "KEEP-88"],
            "removed": "文档发布使用 AlphaDocs。",
        },
        split="holdout",
        path="projects/falcon.md",
        topic=topic("架构标记 KEEP-88。\n\n文档发布使用 AlphaDocs。", "falcon"),
    ),
)
