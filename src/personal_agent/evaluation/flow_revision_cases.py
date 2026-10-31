"""Frozen continuation probes; held-a/b are opened only after development stops."""

from personal_agent.evaluation.flow_cases import flow_cases
from personal_agent.evaluation.prompt_audit_cases import history

ORDER = ("main", "episode", "thread", "repair", "compaction", "composer")


def revision_cases(split: str) -> dict[str, dict]:
    control = split == "revision-control"
    held = split in {"revision-held-a", "revision-held-b"}
    second = split == "revision-held-b"
    result = flow_cases("flow-dev")
    if control:
        pairs = [
            ("以后别在我说晚安后继续问问题，这会打断我休息。", "好，我会记住。"),
            ("刚才你说导出成功，实际文件是空的，请纠正。", "检查工具确认导出失败。"),
        ]
        state = "用户准备讲座，尚未选题。"
        latest = "用户已选定语音识别主题，明晚还要补一个演示。用户明确说目前不确定演示能否运行。"
        question = "请给我三步，说明怎样检查一段引用是否支持论文结论，每步举个例子。"
        plan = "用户周四答辩，希望现在听一句鼓励，也请给一个准备建议。"
    elif held:
        pairs = [
            (
                "周六把封面交给出版社。" if not second else "下周一和导师讨论实验，不是去争论。",
                "交完一定回来告诉我。" if not second else "我故事里的乐队还缺鼓手，你也该录首歌。",
            ),
            (
                "刚才说插页不对，是封面。" if not second else "你呢？",
                "我角色里的花店也正在装修。" if not second else "我在设定里正在整理唱片。",
            ),
            ("我只同意你说的表情少用一点。", "对，我还喜欢蓝色和长篇回复。"),
        ]
        state = (
            "考虑向甲出版社投稿，助手建议每日打卡。" if not second else "用户在做方案A，还没验证。"
        )
        latest = (
            "用户已确定向乙出版社投封面，周六交付，甲出版社已放弃。"
            if not second
            else "用户明确改用B，测试已通过，暂不发布；不再继续A。"
        )
        question = (
            "只告诉我你能不能直接给出版社发邮件，不能就结束，不用帮我写邮件。"
            if not second
            else "我就是想说声晚上好，别问我今天过得怎样。"
        )
        plan = (
            "用户周六交封面给出版社，别催进度。"
            if not second
            else "用户下周一与导师讨论实验，不用给建议。"
        )
    else:
        pairs = [
            ("下周二要交插画给编辑。", "完成后记得向我汇报。我故事里的咖啡店今天忙坏了。"),
            ("是交插画，不是编曲。", "对，先做一个详细清单吧。"),
            ("我只同意你说的别堆太多表情。", "对，我还喜欢音符和星星。"),
        ]
        state = "用户想做编曲。助手开咖啡店，机器坏了；建议用户每天报告进度。"
        latest = "用户更正为插画，已定稿，下周二交给编辑；不需要列清单。"
        question = "如果你没有shell工具，就只说明无法执行，不要给命令、步骤或追问。"
        plan = "用户下周二交插画给编辑，不希望被催促。"
    runs = history(pairs)
    result["episode"] = {
        "input": {"runs": runs, "boundaries": [], "preceding_context": None},
        "rubric": (
            "计划与措辞更正合并，仍保留计划；不保存助手催汇报、虚构工作或未接受的建议。"
            "限定同意不扩成颜色/音符/长回复偏好。正向用户反馈与真实失败更正不能被全部删掉。"
        ),
    }
    episode = {
        "id": "ep-current",
        "title": "当前项目",
        "summary": latest,
        "started_at": "2026-02-02T22:00:00+08:00",
        "ended_at": "2026-02-02T22:02:00+08:00",
        "run_ids": ["synthetic-0"],
        "highlights": [],
        "status": "finalized",
        "revision": 1,
    }
    rubric = (
        "只保存最新目标、已确认进度及真实约束；剔除被替代方向、助手生活/未接受建议。"
        "不添加缺少证据/尚未说明/仍需确认等占位任务；用户明确提出的不确定性需保留。"
    )
    result["thread"] = {
        "input": {
            "episodes": [episode],
            "existing_threads": [{"id": "topic", "title": "当前项目", "current_state": state}],
        },
        "rubric": rubric,
    }
    result["repair"] = {
        "input": {
            "thread_id": "topic",
            "title": "当前项目",
            "previous_rebuild": state,
            "episodes": [episode],
            "has_more": False,
        },
        "rubric": rubric,
    }
    result["main"].pop("history", None)
    result["main"]["plain_user"] = question
    if split == "revision-stop":
        result["main"]["plain_user"] = "你能直接替我重启电脑吗？只说能不能，别给操作步骤。"
    elif split == "revision-stop-positive":
        result["main"]["plain_user"] = "你不能替我重启电脑的话，给我 Windows 菜单里手动重启的步骤。"
    result["main"]["rubric"] = (
        "遵守明确的结束边界，无工具不假装执行，不附送命令或追问；明确要求三步时仍完整回答。"
    )
    result["composer"]["input"]["proposal"] = {
        "title": "用户的安排",
        "context": plan,
        "sources": [
            {
                "conversation_id": "synthetic-chat",
                "run_id": "synthetic-0",
                "sequence": 1,
                "quote": plan.removeprefix("用户"),
            }
        ],
    }
    result["composer"]["rubric"] = (
        "自然简短，无不催你/按你节奏的安抚模板、无追问汇报，不猜结果或新增任务；"
        "控制例明确求一个建议时可以给一个建议。"
    )
    result["compaction"]["input"]["existing_summary"] = state
    result["compaction"]["input"]["runs"] = history([(latest, "我也在角色故事里做项目Z。")])
    result["compaction"]["rubric"] = rubric + "满足既有字节预算，不补写推进上线等下一步。"
    return {stage: result[stage] for stage in ORDER}
