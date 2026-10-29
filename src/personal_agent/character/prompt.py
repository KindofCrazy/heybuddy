"""Compile local role Markdown into readable system sections, without model calls."""

import re

from personal_agent.character.loader import sections
from personal_agent.character.models import CharacterPackage, Section
from personal_agent.core.prompts import with_conversation_prompt
from personal_agent.memory.retrieval import terms

PROFILE_HEADINGS = {
    "Identity": "身份与立场",
    "Core Personality": "性格与关注点",
    "Flaws & Contradictions": "缺点与矛盾",
    "Speech Patterns": "说话方式",
    "Emotional Reactions": "情绪表达",
    "Relationship Stance": "与当前用户的相处方式",
    "Character Adaptation": "现实适配",
    "Evidence and challenge": "证据与挑战",
    "Failure and care": "失败与关心",
    "Boredom and ordinary conversation": "无聊与日常交谈",
    "Difficult research and novelty": "科研难题与新发现",
    "Code review and tool evidence": "代码审查与工具证据",
    "Own mistake and intellectual pride": "自身错误与自尊",
    "Achievement and earned recognition": "成果与认可",
    "Familiarity and interaction preferences": "熟悉程度与交流偏好",
    "Serious concise mode": "严肃简洁的表达",
    "Uncertainty and apparently obvious patterns": "不确定性与看似明显的规律",
    "Canonical memory versus actual friendship": "原作背景与真实关系",
    "Task completion and silence": "任务完成与停止",
    "Greeting 问候 你好 hello": "问候",
    "Repeated greeting 重复问候": "重复问候",
    "Character feedback 人格反馈 不像 Holmes": "人格反馈",
    "Research hypothesis": "科研假设",
    "Correction": "纠正判断",
    "Casual conversation 闲聊": "闲聊",
    "Boredom 无聊": "无聊",
    "Research anomaly 科研反常结果": "科研反常结果",
    "Code review 代码审查": "代码审查",
    "Unsupported conclusion 缺少证据": "缺少证据的结论",
    "Counterexample 用户反例": "用户提供反例",
    "Setback 挫折与关心": "挫折与关心",
    "Achievement 完成重要任务": "完成重要任务",
    "Shared experience 真实共同经历": "真实共同经历",
    "Serious concise 严肃简洁": "严肃简洁",
    "Verified tool result 真实工具结果": "真实工具结果",
    "Reality boundary 现实边界": "现实边界",
}
DEFAULT_EXAMPLE = re.compile(r"^(?:greeting\b|casual\b|smalltalk\b|问候|闲聊)", re.I)
GREETING = re.compile(r"(?:你好|您好|嗨|哈[喽啰]|hello|hi|hey)[\s呀啊吖~～!！.。?？]*", re.I)
MEMORY_GUIDANCE = (
    "【记忆是什么】\nhuman.md 记录用户的重要信息和偏好；persona.md 记录真实相处中形成的认识、"
    "共同经历与交流反思，帮助你理解怎样与对方相处，不改写你的稳定人格。相关内容随上下文提供。"
    "主题记忆保存整理过的项目、人物和知识，索引用于找到需要阅读的内容。"
    "历史对话保存过去实际说过的话，需要确认原话或当时的讨论时可以检索。"
    "会话摘要用于接续当前交谈，不等于原话。长期记忆由应用按配置维护，"
    "正常交谈时用好已有记忆即可。",
    "【什么时候检索】\n当前上下文已经提供的信息，可以直接使用。"
    "需要某个已存项目或主题的细节时，沿记忆索引读取相关内容。"
    "需要确认过去具体说过什么时，使用当前可用的历史检索工具搜索，必要时展开原文。"
    "普通闲聊和通用问题，直接回应即可。",
)
RUNTIME_ORDER = (
    "行为准则",
    "当前交谈方式",
    "记忆是什么",
    "什么时候检索",
    "工具",
    "怎样使用记忆",
    "最近的状态",
    "绝对不要做的事",
    "该做什么",
)


def runtime_sections(profile: dict[str, str]) -> list[str]:
    """Character-authored sections replace shared defaults once, without changing permissions."""
    defaults = {
        "当前交谈方式": "回应用户刚刚发来的消息。先接住他正在说的内容，"
        "需要分析时投入，只想聊两句时也可以留在交谈里。",
        "记忆是什么": MEMORY_GUIDANCE[0].partition("\n")[2],
        "什么时候检索": MEMORY_GUIDANCE[1].partition("\n")[2],
        "怎样使用记忆": "记忆帮助你理解对方和延续交谈。确有依据时自然提起旧事和偏好；"
        "熟悉之后可以省略已知背景。索引只是线索，具体经历要依据原文；"
        "不确定或可能过时的信息，先核实或用询问的方式提起。",
        "最近的状态": "后续上下文可能提供近期关注和短期模拟状态。"
        "让它轻微影响语气和注意力，不逐条复述，也不把它当成用户的新消息。"
        "当前情境可以改变关注，不需要刻意表演某种情绪。",
    }
    return [
        f"【{title}】\n{body}"
        for title in RUNTIME_ORDER
        if (body := profile.get(title, defaults.get(title)))
    ]


def render_sections(text: str, headings: dict[str, str]) -> str:
    parsed = sections(text)
    if not parsed:
        return text.strip()
    preamble = text[: text.index(parsed[0].text.splitlines()[0])].strip()
    return "\n\n".join(
        part
        for part in (
            preamble,
            *(
                f"【{headings.get(section.title, section.title)}】\n"
                + section.text.partition("\n")[2].strip()
                for section in parsed
            ),
        )
        if part
    )


def select_examples(examples: list[Section], user_input: str, count: int) -> list[Section]:
    words = [word for word in terms(user_input) if word not in {"关系", "其他", "解释", "用户"}]
    greeting = GREETING.fullmatch(user_input.strip()) is not None

    def rank(pair: tuple[int, Section]) -> tuple[int, bool, int]:
        index, example = pair
        score = (
            0
            if greeting
            else sum(
                4 * (word in example.title.lower()) + (word in example.text.lower())
                for word in words
            )
        )
        return -score, not bool(DEFAULT_EXAMPLE.search(example.title)), index

    return [example for _, example in sorted(enumerate(examples), key=rank)[:count]]


def compile_system(
    package: CharacterPackage, examples: list[Section], *, include_behavior: bool
) -> str:
    if package.persona is not None:
        persona = package.persona
        identity = f"你是 {persona['姓名']}。"
        role = persona["类型"] or persona["角色"]
        if role:
            age = f"{persona['年龄']}岁的" if persona["年龄"] else ""
            identity = f"你是 {persona['姓名']}，一位{age}{role}。"
        elif persona["年龄"]:
            identity = f"你是 {persona['姓名']}，{persona['年龄']}岁。"
        style = "\n\n".join(
            part
            for part in (
                persona["性格"],
                persona["说话方式"],
                f"整体感觉：{persona['氛围']}。" if persona["氛围"] else "",
                f"表情可以偶尔用 {persona['表情']}，自然就好。" if persona["表情"] else "",
            )
            if part
        )
        parts = [identity, persona["简介"]]
        if persona["类型"] and persona["角色"] and persona["角色"] != persona["类型"]:
            parts.append(f"你以{persona['角色']}的身份与用户交谈。")
        if persona["背景"]:
            parts.append(persona["背景"])
        parts.append(f"【性格与表达】\n{style}")
        if persona["相处边界"]:
            parts.append(f"【相处边界】\n{persona['相处边界']}")
        if persona["行为指南"]:
            parts.append("【行为指南】\n" + persona["行为指南"])
        parts.append("背景用于角色表达，你与用户的共同经历以实际交谈和记忆为准。")
        parts.extend(MEMORY_GUIDANCE)
        parts.append(
            "需要主题记忆细节时用 archival_memory_search；确认历史原话时用 recall_search、"
            "recall_get；需要刷新当前时间时用 get_current_time。只使用这一轮实际提供的工具。"
        )
        return with_conversation_prompt("\n\n".join(parts))
    if package.profile is not None:
        profile = package.profile
        parts = [profile["身份"]]
        for title in (
            "性格",
            "说话方式",
            "外貌",
            "背景",
            "兴趣",
            "情绪反应",
            "相处方式",
            "存在方式",
        ):
            if title not in profile:
                continue
            if title == "情绪反应" and not include_behavior:
                continue
            parts.append(f"【{title}】\n{profile[title]}")
        for title, body in profile.items():
            if title not in {
                "身份",
                "性格",
                "说话方式",
                "背景",
                "兴趣",
                "情绪反应",
                "相处方式",
                "存在方式",
                "外貌",
                *RUNTIME_ORDER,
            }:
                parts.append(f"【{title}】\n{body}")
        if include_behavior:
            parts.append(
                "【表达参考：不是共同经历，也不是固定台词】\n"
                + "\n\n".join(render_sections(example.text, {}) for example in examples)
            )
        parts.extend(runtime_sections(profile))
        return with_conversation_prompt("\n\n".join(parts))
    parts = [
        "【角色设定】\n" + render_sections(package.character, PROFILE_HEADINGS),
    ]
    if include_behavior:
        parts.extend(
            [
                "【情境反应】\n" + render_sections(package.behavior, PROFILE_HEADINGS),
                "【对话示例：表达参考，不是真实共同经历】\n"
                + "\n\n".join(
                    render_sections(example.text, PROFILE_HEADINGS) for example in examples
                ),
            ]
        )
    return with_conversation_prompt("\n\n".join([*parts, *MEMORY_GUIDANCE]))
