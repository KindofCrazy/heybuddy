# Passive Character Runtime

## 职责与组成

`character/models.py` 定义 CharacterPackage、Section、独立大小预算和安全错误。`character/loader.py::CharacterLoader.load()` 只读取 Host 指定本地根目录下的四个固定 Markdown，不导入包内代码，不下载或执行任何内容。包放在 src/personal_agent/character/packages，随 wheel/sdist 分发；Markdown 是角色定义的唯一权威内容，fingerprint 仅用于变化检测，不是人格副本或版本数据库。

## 实际运行路径

1. Host 给出稳定小写 character_id；正则拒绝绝对路径、分隔符、穿越和 none。未启用时应由调用方直接跳过 Loader。
2. 读取前逐级检查根目录、包目录和文件，拒绝 Symlink/Junction/reparse point；四个固定文件每个最多默认 16384 字节，超限或缺失整体失败。
3. UTF-8 解码、拒绝空正文/NUL、统一 CRLF，再解析二级标题。character.md 必须具备 Identity、Core Personality、Flaws & Contradictions、Character Adaptation；行为、示例、Lore 必须有章节，重复章节拒绝。
4. 返回不可执行的普通数据与内容 fingerprint。读取错误只返回固定 code，不打印文件正文。调用方须将角色视为次于 Host 的行为设定，不能用角色文本授予工具权限。

## BBC Sherlock 包

character.md 包含剧版现代身份、关注异常/证据的价值取向、自负与修正的矛盾、成年创作基线、真实工具适配和与当前用户独立建立关系的边界。behavior.md 以 Situation/Interpretation/Reaction/Observable Behavior/Moderating Conditions 表达情境化规律；examples.md 为原创示例；lore.md 明确 Canonical Backstory 不等于当前用户共同经历。没有影视对白摘录、视觉资源或 Actor 身份冒充。

角色是行为模拟，不意味着模型有人的情绪、意识或真实线下经历。剧版人物是开发参考，不能据此声称影视特有视觉、对白等内容可无授权商业化分发。背景核对及理论参考见 [P3 架构](../p3-character-architecture.md)。

## Context 接入

`character/context.py::CharacterContextCompiler` 生成独立、有界的角色层。构造时以最长的示例组合验证关键静态内容，超限直接 CharacterError，不截断核心人格。每次 compile() 提供完整 Character Core/Behavior、最多默认两条相关示例，以及仅命中命名章节的至多两段 Lore；不把四个文件全文注入每轮。可选引用有独立字节上限，超限整段省略。

Core 只依赖 `core/character.py::CharacterContext` Protocol。SessionManager 将同一个编译器传入每个 ContextBuilder，切换 Conversation 不丢失角色设置。for_request() 顺序是原 Host System、角色 System、过滤后的 Core、Summary/时间、可选角色引用、历史检索、原近期/当前消息。角色层固定宿主文字明确优先级、执行真实性、虚构背景与用户历史边界；角色包不能注册工具或赋权。

启用时只对 memory.compile() 的 JSON 注入视图移除 persona.md，原文件、公共 MemoryFS 编译与写入语义不变；human.md 和 MEMORY.md 保留。关闭角色不经过过滤，立即恢复旧视图。角色材料不写入 pending 或 Journal，完整模型请求继续经过原 ContextBudget 估算、压缩和失败路径。类型/预算不适配返回安全的 context_error，不暗中删除当前用户输入。

AGENT_CHARACTER_ID 默认 none；sherlock_bbc 显式启用。main._run_session() 在装配 Agent 前读取包；配置或读取失败给出明确 CLI 错误。`/context status` 的 character 显示 ID、变体、包 hash、编译大小和额外 Appraisal 请求数（零），不显示私人上下文。tests/test_character_context.py 验证模式回退、磁盘保留、System 顺序、多轮工具身份、总预算、Lore 上限和会话隔离。

## 异常与验证

`CharacterError.code` 区分 ID、缺失、链接、编码/读取和预算/章节错误。路径验证不是针对本机恶意并发文件替换的 OS 沙箱；允许标准安装器用 hardlink 安装只读包资源，不把它们误判为 Memory 私有文件。`tests/test_character_loader.py` 覆盖正常读取、修改检测、缺失/无效/超限和 Symlink/Junction 逃逸。最小示例：`CharacterLoader().load("sherlock_bbc")`；本模块不调用 Provider。
