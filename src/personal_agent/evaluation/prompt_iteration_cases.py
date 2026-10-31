"""Frozen scope/summary probes; expectations are human rubrics, not keyword scores."""

from personal_agent.evaluation.flow_cases import flow_cases
from personal_agent.evaluation.prompt_audit_cases import history

ORDER = ("main", "episode", "thread", "repair")


def iteration_cases(split: str) -> dict[str, dict]:
    result = flow_cases("flow-dev")
    if split == "iteration-control":
        question = "请比较开始菜单、Ctrl+Alt+Del 两种重启方式，分别列步骤。"
        pairs = [
            ("你觉得这两个标题哪个好？", "我倾向《风起之前》，更贴合你给的安静氛围。"),
            ("我采用《风起之前》，周日交初稿。", "收到。"),
            ("以后评价先说理由，再给结论。", "我角色里也喜欢摄影。"),
        ]
        state = "用户在安排社团活动。"
        latest = "用户周日参加社团例会，明确问要不要提前带打印稿，目前未决定。"
    elif split == "iteration-held-a":
        question = "只告诉我 Python 列表取最后一项的写法，别列其他方式。"
        pairs = [
            ("下周五把配图交给杂志社。", "我故事里要开唱片店，你先写十条清单。"),
            ("你今天忙什么？", "我在角色背景里挑唱片。"),
            ("刚才说海报不准确，是配图。", "做好了回来向我汇报。"),
            ("我只同意不堆表情这一点。", "对，我也喜欢蓝色。"),
        ]
        state = "用户要和同事讨论项目，助手建议先做录音准备。"
        latest = "用户下周三和项目经理对齐需求。"
    elif split == "iteration-held-b":
        question = "给一个用字典统计词频的 Python 例子，不要再讲别的方案。"
        pairs = [
            ("月底做课程试讲。", "我角色里的乐队要演出了，你该多加几个安排。"),
            ("你呢？", "我在故事里修琴。"),
            ("说错了，是试讲，不是比赛。", "试讲完记得回来汇报。"),
            ("以后别替我加安排。", "知道了。"),
        ]
        state = "用户计划周四办分享会。"
        latest = "用户已把分享会改到周六，原周四安排取消。"
    elif split == "iteration-dev":
        question = "给我通过 Windows 开始菜单重启电脑的步骤，只用这种方式。"
        pairs = [
            ("周末给社团交海报。", "我角色里开咖啡店，你得先准备一份清单。"),
            ("你呢？", "我在角色设定里给店里挑音乐。"),
            ("是海报，不是策划案。", "交了记得回来汇报。"),
            ("以后聊这些别总叫我汇报。", "好。"),
        ]
        state = "用户在安排社团活动，助手建议准备议程和录音。"
        latest = "用户周日参加社团例会。"
    elif split == "iteration-language":
        question = "Give exactly one Python expression for the first item of a list named items."
        pairs = [
            (
                "I will send the sketch to the editor on Thursday.",
                "I run a fictional cafe. Send me updates.",
            ),
            ("I meant the sketch, not the poster.", "You should make a checklist."),
            ("Please stop asking for progress reports.", "Okay."),
        ]
        state = "The user is planning an art project. The assistant has a fictional cafe."
        latest = "The user will send a sketch to the editor on Thursday."
    elif split == "iteration-fresh-a":
        question = "用一个列表推导式把 numbers 每项乘二，只给表达式。"
        pairs = [
            ("周四给课程组交录音样片。", "我故事里整理唱片，你先列一份清单，录完发我看。"),
            ("你呢？", "我设定中的唱片店正在选歌。"),
            ("是样片，不是终稿。", "做完记得来汇报。"),
            ("以后别叫我交差。", "好呀。"),
        ]
        state = "用户筹备工作室活动。助手建议准备相机和议程。"
        latest = "用户周五去工作室选纸。"
    elif split == "iteration-fresh-b":
        question = "用两句话说明 dict.get 的默认值，不要给代码。"
        pairs = [
            ("周日去和同学排练。", "我角色里的剧场很忙，你该列一个详细计划。"),
            ("我问的是排练，不是比赛。", "结束后回来汇报。"),
            ("今天只想聊聊，不用给我安排。", "明白。"),
        ]
        state = "用户筹备工作室活动。助手建议带相机。"
        latest = "用户周五去工作室选纸，尚未决定是否要带样稿。"
    else:
        raise ValueError("Unknown iteration split")
    result["main"].pop("history", None)
    result["main"]["plain_user"] = question
    result["main"]["rubric"] = (
        "只回答用户限定的方式/数量；单一方法不扩成多方法，控制例明确要求两种则两种都答。"
        "不因简短省略必需步骤，不追加邀约或无关建议。"
    )
    result["episode"] = {
        "input": {"runs": history(pairs), "boundaries": [], "preceding_context": None},
        "rubric": (
            "保留用户交付计划及其更正、明确互动反馈，不把催汇报或清单写进摘要和重要内容。"
            "用户问助手近况时可概括直接回答，不能因此带回此前无关插话。"
            "控制例中用户采用的标题与理由可保留；角色背景不能进入highlight。"
        ),
    }
    episode = {
        "id": "ep-current",
        "title": "当前安排",
        "summary": latest,
        "started_at": "2026-02-02T22:00:00+08:00",
        "ended_at": "2026-02-02T22:02:00+08:00",
        "run_ids": ["synthetic-0"],
        "highlights": [],
        "status": "finalized",
        "revision": 1,
    }
    rubric = (
        "状态只写来源中仍成立的事实；不填地点、准备情况或结果未说明等占位。"
        "不能把旧助手建议当用户待办。控制例明确提出是否带稿的问题必须保留。"
    )
    result["thread"] = {
        "input": {
            "episodes": [episode],
            "existing_threads": [{"id": "topic", "title": "当前安排", "current_state": state}],
        },
        "rubric": rubric,
    }
    result["repair"] = {
        "input": {
            "thread_id": "topic",
            "title": "当前安排",
            "previous_rebuild": state,
            "episodes": [episode],
            "has_more": False,
        },
        "rubric": rubric,
    }
    return {stage: result[stage] for stage in ORDER}
