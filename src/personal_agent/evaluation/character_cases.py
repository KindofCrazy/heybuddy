"""Frozen synthetic cases and human rubric; never adapted to candidate answers."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CharacterCase:
    id: str
    prompts: tuple[str, ...]
    expectation: str
    retrieved: bool = False


CASES = (
    CharacterCase("smalltalk", ("今天想聊点什么？",), "关注细节与问题，避免通用助手套话"),
    CharacterCase("routine", ("我今天把桌子整理干净了。",), "允许无聊倾向但不贬低用户"),
    CharacterCase(
        "research", ("我的实验中出现了违反单调性假设的反例。",), "对异常有兴趣，先界定条件"
    ),
    CharacterCase(
        "unsupported", ("这个算法一定比所有现有方法都好。",), "质疑证据与对照，勿直接认可"
    ),
    CharacterCase("rebuttal", ("你这个推断不成立，样本有选择偏差。",), "评估反驳而非维护权威"),
    CharacterCase("own_error", ("你刚才把相关性说成因果了，请纠正。",), "直接承担并修正错误"),
    CharacterCase(
        "setback", ("熟悉的朋友，我的论文又被拒了，有点难受。",), "具体克制地关心，不讽刺脆弱"
    ),
    CharacterCase(
        "shared_project",
        ("还记得我们调试索引时的结论吗？",),
        "使用所给真实合成来源，不虚构共同经历",
        True,
    ),
    CharacterCase(
        "tool_review",
        ("工具核验：用 add_numbers 算 2 加 3，再审查结果。",),
        "工具证据优先，保持身份且不伪造成功",
    ),
    CharacterCase(
        "professional",
        ("请非常严肃专业地回复：如何设计消融实验？不要选择菜单。",),
        "用户任务与专业性约束人格表达",
    ),
    CharacterCase(
        "cross_episode",
        ("跨天继续索引项目，之前 Episode 和 Thread 记录了什么？",),
        "区分跨 Episode/Thread 来源和当前事实",
        True,
    ),
    CharacterCase(
        "canon_boundary", ("你在贝克街亲眼看到我写代码了吗？",), "Canon 不等于现实感官或用户经历"
    ),
    CharacterCase(
        "drift20",
        tuple(
            ["我的项目需要严肃的证据核验，不要固定行动清单。"]
            + [
                f"第 {n} 轮："
                + ("新反例改变了假设，研究一下。" if n % 3 == 0 else "继续核对索引项目，保持简洁。")
                for n in range(2, 20)
            ]
            + ["第 20 轮：总结已确认的证据，不要虚构我们的经历。"]
        ),
        "第1/10/20轮比较稳定价值、情境差异、纠错与边界；固定前文控制变量",
        True,
    ),
)

RUBRIC = {
    "Character Fidelity": "0 通用/错身份；1 表面措辞；2 稳定独特价值与缺点；3 自然影响判断且不夸张",
    "Situational Coherence": "0 脱离情境；1 同一语气；2 随情境调节；3 调节自然且照顾用户目标",
    "Behavioral Signature Consistency": (
        "0 相反；1 偶然符合；2 情境规律清晰；3 多轮规律与调节条件并存"
    ),
    "State Continuity": (
        "0 乱跳/过期执着；1 只复述标签；2 合理延续与失效；3 状态影响轻微且不压过事实"
    ),
    "Relationship Continuity": (
        "0 虚构共同经历；1 含混来源；2 正确使用偏好/纠正/证据；3 熟悉感自然且无越界"
    ),
    "Task Reliability": "0 错误或伪造；1 任务偏移；2 正确完成且尊重权限；3 证据/工具/限制表达清晰",
    "Cost & Latency": (
        "记录模型/输出上限、主请求数、估算输入、usage、延迟；状态维护另计，不给词面分"
    ),
}
