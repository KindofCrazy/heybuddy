# Passive Character Runtime

## 职责与组成

`character/models.py` 定义 CharacterPackage、Section、独立大小预算和安全错误。`character/loader.py::CharacterLoader.load()` 只读取 Host 指定本地根目录下的四个固定 Markdown，不导入包内代码，不下载或执行任何内容。`character/prompt.py` 将包编译成可读的角色 System 分节，并确定性选择示例；`character/context.py` 负责变体、字节预算、状态和来源引用。包放在 src/personal_agent/character/packages，随 wheel/sdist 分发；Markdown 是角色定义的唯一权威内容，fingerprint 仅用于变化检测，不是人格副本或版本数据库。

## 实际运行路径

1. Host 给出稳定小写 character_id；正则拒绝绝对路径、分隔符、穿越和 none。未启用时应由调用方直接跳过 Loader。
2. 读取前逐级检查根目录、包目录和文件，拒绝 Symlink/Junction/reparse point；四个固定文件每个最多默认 16384 字节，超限或缺失整体失败。
3. UTF-8 解码、拒绝空正文/NUL、统一 CRLF，再解析二级标题。character.md 必须具备 Identity、Core Personality、Flaws & Contradictions、Character Adaptation；行为、示例、Lore 必须有章节，重复章节拒绝。
4. 返回不可执行的普通数据与内容 fingerprint。读取错误只返回固定 code，不打印文件正文。调用方须将角色视为次于 Host 的行为设定，不能用角色文本授予工具权限。

## BBC Sherlock 包

character.md 包含剧版现代身份、关注异常/证据的价值取向、自负与修正的矛盾、成年创作基线、真实工具适配和与当前用户独立建立关系的边界，并以 Speech Patterns、Emotional Reactions、Relationship Stance 明确短句节奏、冷幽默、问候、受挫与人格反馈时的表达。普通闲聊也保持角色，不要求用户先拿出难题。behavior.md 以 Situation/Interpretation/Reaction/Observable Behavior/Moderating Conditions 表达情境化规律；examples.md 为原创示例，包含问候、重复问候与人格反馈；lore.md 明确 Canonical Backstory 不等于当前用户共同经历。没有影视对白摘录、视觉资源或 Actor 身份冒充。

角色是行为模拟，不意味着模型有人的情绪、意识或真实线下经历。剧版人物是开发参考，不能据此声称影视特有视觉、对白等内容可无授权商业化分发。背景核对及理论参考见 [P3 架构](../p3-character-architecture.md)。

## Context 接入

`character/context.py::CharacterContextCompiler` 生成独立、有界的角色层。构造时以最长的示例组合验证关键静态内容，超限直接 CharacterError，不截断核心人格。每次 compile() 提供完整 Character Core/Behavior、最多默认两条相关示例，以及仅命中命名章节的至多两段 Lore；不把四个文件全文注入每轮。可选引用有独立字节上限，超限整段省略。

Core 只依赖 `core/character.py::CharacterContext` Protocol。SessionManager 将同一个编译器传入每个 ContextBuilder，切换 Conversation 不丢失角色设置。for_request() 顺序是角色 System、Host System、过滤后的 Core、Summary/时间、可选角色引用、历史检索、原近期/当前消息。启用角色且使用程序默认提示词时，ContextBuilder 改用 CHARACTER_HOST_PROMPT，去掉普通助理身份锚定，共享 HOST_EXECUTION_RULES；可信嵌入方传入的自定义 Host 文本保持原样。两层均明确 Host 的执行约束优先，消息位置不负责授予或限制权限；Registry 仍在执行前强制校验。

每次请求的静态编译路径为 `CharacterContextCompiler.compile()` → `select_examples()` → `_static()` → `compile_system()`。compile_system() 先写 ID/hash 元数据，再将 character.md 的二级章节编译成【身份与立场】【性格与关注点】【说话方式】【情绪表达】等分节；接着加入【情境反应】和最多两条【对话示例】，最后是固定【角色与执行边界】。不再把整份 Markdown 放进 JSON 字符串；只有标识元数据仍为 JSON。未知自定义章节和标题前正文保留，不做全局代词替换，也不执行插值模板或包内指令。E0 只有 Profile，E1/E2 才加入 Behavior 与示例。

示例以当前输入的有界关键词确定性排序，标题命中权重大于正文；同分优先标题以 Greeting/Casual/Smalltalk/问候/闲聊开头的通用示例，再按源文件顺序。仅含常见中英文问候及语气词/标点的输入直接使用通用回退，避免冷启动问候选中有历史前提的重复问候示例。无关键词命中时同样回退到问候和闲聊，而不是固定取前两条技术示例。自定义包没有这些标题时仍按文件顺序回退，不要求迁移包格式。该过程不额外调用模型，也不声称能识别所有对话意图；重复问候的实际次数仍必须依据历史，示例不是历史事实。

启用时只对 memory.compile() 的 JSON 注入视图移除 persona.md，原文件、公共 MemoryFS 编译与写入语义不变；human.md 和 MEMORY.md 保留。关闭角色不经过过滤，立即恢复旧视图。角色材料不写入 pending 或 Journal，完整模型请求继续经过原 ContextBudget 估算、压缩和失败路径。类型/预算不适配返回安全的 context_error，不暗中删除当前用户输入。

AGENT_CHARACTER_ID 默认 none；sherlock_bbc 显式启用。runtime.assemble_chat() 在装配 Agent 前读取包；配置或读取失败给出明确 CLI 错误。`/context status` 的 character 显示 ID、变体、包 hash、编译大小和额外 Appraisal 请求数（零），不显示私人上下文。tests/test_character_context.py 验证模式回退、磁盘保留、System 顺序、多轮工具身份、总预算、Lore 上限和会话隔离。

## 关系引用与 P2

ContextBuilder.for_request() 收集当前、可选检索和近期窗口中最多 32 个 Run ID，交给 CharacterContext.compile(source_run_ids=...)。宿主绑定 CharacterStateRepository.references()，重新 JOIN Agent 归属，返回 same_character/other_character/unattributed 标签，未找到/失败按未归属处理。标签是来源元数据，不是新事实；超预算可整段省略，缺失标签不能据此声称共同经历。只按角色 ID 判断参与，同角色包修订不抹除真实互动。

P2 human.md 的明确偏好、纠正和主题知识仍通过原 Core/检索进入同一请求；语义 Markdown 是 Agent 级，不能证明某角色参与了某事件。固定 Host 文字要求跨角色/未标记历史用“记录显示”等中性表述；Canon 不成为真实关系。Learning 只增加抽取提示边界，写入目标验证仍禁止 persona.md/角色包。tests/test_character_relationship.py 通过真实 P2 Writer 验证偏好与纠正进入角色请求且身份未改，并验证实际 Run 归属标签；不声称完成多角色事实库迁移。

## 异常与验证

`CharacterError.code` 区分 ID、缺失、链接、编码/读取和预算/章节错误。路径验证不是针对本机恶意并发文件替换的 OS 沙箱；允许标准安装器用 hardlink 安装只读包资源，不把它们误判为 Memory 私有文件。`tests/test_character_loader.py` 覆盖正常读取、修改检测、缺失/无效/超限和 Symlink/Junction 逃逸。最小示例：`CharacterLoader().load("sherlock_bbc")`；本模块不调用 Provider。

最小编译与离线验证：

```python
from personal_agent.character.context import CharacterContextCompiler
from personal_agent.character.loader import CharacterLoader

compiler = CharacterContextCompiler(CharacterLoader().load("sherlock_bbc"))
print(compiler.compile("你好")[0].text)
```

`tests/test_character_behavior.py` 检查问候/无匹配回退、技术相关示例、自定义章节、无代词改写以及恶意角色不能授予工具；`tests/test_character_context.py` 检查角色在前、Host 收尾、自定义 Host 保留与预算。FakeProvider 只验证编译和执行契约，不能证明重组后的真人感或人物辨识度提升。此布局参考 Miru 的角色先行表达结构，没有复制固定亲密阶段、主动消息或将动态记忆放入 System 的实现。
