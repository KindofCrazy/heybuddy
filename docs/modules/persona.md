# 简化角色设定

## 职责与架构组成

当前开发使用 OpenPersona 的 Luna 预设。`character/packages/luna/persona.json` 是角色设定来源，`soul/behavior-guide.md` 是它引用的本地行为指南；自然语言已整理为中文，来源与适配记录见同目录 SOURCE.md，MIT 声明保存在 LICENSE。角色正文最终接上独立的 `prompts/conversation.md`，不引入 OpenPersona 的生成器、运行时、依赖或自动安装。

Luna 指南在上游四个主题的基础上，吸收用户提供的 Miru 情绪反应与说话习惯，补充稳定接纳、直接表达、具体关心和认可，以及可爱或符合音乐、夜晚、代码兴趣的 emoji。具体反应是表达参考，编译器仍一次性完整注入；没有新增情绪状态任务、工具或发送能力。

`character/persona.py` 只解析 OpenPersona 的 Soul 描述字段；`character/loader.py` 负责固定文件读取、路径保护、大小限制和内容指纹；`character/prompt.py::compile_system()` 将描述编译为自然语言；`character/context.py` 复用现有单一 System、诊断与预算。`runtime.py::assemble_chat()` 将它接到现有 CLI、QQ 和只读 Observatory，不为 JSON 角色创建 CharacterStateService。

## 实际运行路径

1. `config.load_settings()` 从可信配置取得 AGENT_CHARACTER_ID。开发示例为 luna，Settings 的库级默认仍是 none；ROOT 可以指向可信本地角色目录。
2. `runtime.assemble_chat()` → `CharacterLoader.load()` 校验 ID 和目录，优先检查 persona.json；只要该文件存在，损坏时就报错，不回退为同目录的旧 soul.md。
3. Loader 有界读取 UTF-8 文件，默认每个文件最多 16384 字节，拒绝空白、非法编码、NUL、Symlink/Junction/reparse point。
4. `persona.parse_persona()` 使用标准库 JSON 和 Pydantic 验证 Soul。最低要求是 identity 的 personaName、slug、bio 与 character 的 personality、speakingStyle；这些字段必须是非空字符串。角色、年龄、氛围、表情、背景、边界和行为指南为可选描述。不要求固定 Markdown 章节、对话示例或 Lore。
5. behaviorGuide 可以是直接的文本；文件引用仅支持上游预设使用的 `file:soul/behavior-guide.md`。Loader 读取并校验这一固定本地文件，不按任意路径或 URL 追踪内容。指纹包括实际读取的 JSON 与指南正文。
6. CharacterPackage.persona 保存白名单字段的内存解析结果，没有另一份持久角色库。character/behavior 仅为兼容数据接口派生，examples/lore 为空。
7. `SessionManager.context()` → `ContextBuilder.prepare()/for_request()` → `CharacterContextCompiler.compile()` → `compile_system()`：先以“你是 Luna”建立身份，年龄与类型并入这一句，再提供简介、相处定位和背景；性格、说话方式、氛围与表情合并为一段表达说明；边界与完整行为指南随后编入一次。最后提供现有记忆的用途与检索工具，并追加一份通用交谈提示词。没有逐项的年龄、类型、表情等属性标题，指南中的具体情境与例子不再被概括掉。上游组装方式及适配理由见 [对照说明](../openpersona-prompt-assembly.md)。
8. 编译器不为 JSON 角色选择情境示例、检索角色 Lore、读取短期状态或注入角色审计 JSON。E0/E1/E2 对这条简单路径不改变正文；与用户输入无关的稳定设定不会因关键词切换。
9. 编译完整正文 hash 和字节大小后，ContextBuilder 沿原路径提供 Core Memory、时间、会话摘要、历史与当前 Run。自动注入的 Core、时间和摘要是独立的低信任 user 数据消息，历史与当前 Run 保留原消息角色；角色文件不改写会话或 Markdown 记忆。
10. 主模型依旧使用生产工具注册表与逐 Run 权限。后台 Learning/Curator、手动 Reflection 审批和 QQ 被动回复保持现有路径。JSON 角色只保留宿主的 Run 参与记录，不启动角色状态学习，也不会因为上游 heartbeat 等配置增加模型请求或主动消息。

## 异常与边界

不读取或执行 body、faculties、skills、social、evolution、rhythm、additionalAllowedTools；未知 Soul 字段也不注入。这些字段即使保存在源 JSON 中，也不能开通语音、视觉、自拍、联网、支付、命令执行或写权限。实际工具来自 heybuddy 注册表，普通聊天仍提供 get_current_time、archival_memory_search、recall_search、recall_get。

无效 JSON、重复键、非标准常量、缺失/空白/错误类型字段统一返回 invalid_persona_json，错误不携带原始输入。非法指南引用返回 invalid_persona_guide；文件错误复用 CharacterError。任何必需文件加载失败都停止装配，不返回半份设定。路径检查不是针对本机恶意并发替换的操作系统沙箱。

静态提示词超过角色预算直接报错，不静默截断。完整请求仍经过 ContextBudget。文件只在装配时读取，修改后重启生效。Luna 背景是虚构角色设定，不作为用户关系事实；行为指南已将上游“刚听过歌曲、工作中发生的事情”等表述适配为有证据才陈述的现实活动。关系认识仍由 persona.md 与既有证据校验机制维护。

旧 Miru/Sherlock Markdown 包和其状态模块保留兼容，详见 [Character](character.md)。这次不改模型的 reasoning 参数，不通过离线测试宣称自然度已改善。

## 阅读与验证

```python
from personal_agent.character.context import CharacterContextCompiler
from personal_agent.character.loader import CharacterLoader

compiler = CharacterContextCompiler(CharacterLoader().load("luna"))
print(compiler.compile("你好")[0].text)
```

`tests/test_character_persona.py` 验证五字段最小配置、指南完整且仅编入一次、保留描述信息、通用表达位于末尾、JSON 优先级、损坏输入、白名单、文件隔离、指纹和预算；`tests/test_conversation_prompt.py` 验证一条 System 与一份通用提示词；`tests/test_qq_integration.py` 验证 CLI→重启→QQ 共享历史和记忆、原有四个工具及没有角色状态服务。默认测试完全离线，真实对话效果仍需另行授权调用并人工审阅。构建后核对 wheel 中的 persona.json、本地指南、SOURCE.md 和 LICENSE。
