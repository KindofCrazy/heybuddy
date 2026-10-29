# 当前开发角色：Miru

当前先使用独立的 miru 角色包继续开发。`TODO.md` 将 Sherlock 等人格的完善、自然度和多轮评测列为未来工作，已有 sherlock_bbc 包及其测试、Git 版本、真实调用对照保留，不作为当前角色选择。

## 文件与配置

`src/personal_agent/character/packages/miru/soul.md` 是角色文字的唯一来源，包含身份、性格、说话方式、外貌、背景、兴趣、情绪、相处方式和运行时说明；lore.md 保存按需使用的人格灵感与日常素材。设定参考本地 Miru 项目的 soul.md 与共享行为说明，用当前原生中文格式重新编写，运行时不依赖另一个仓库、Flask 或 Miru 的存储。

开发配置显式使用 `AGENT_CHARACTER_ID=miru`，`.env.example` 同步选择此角色；库级 `Settings.character_id` 仍默认 none。`AGENT_CHARACTER_ROOT` 未指定时读取内置包，若自行配置外部根目录，应在对应根目录提供 miru 包。配置通过既有加载器读取，角色在装配时加载，修改或切换后须重启或重新装配；不支持热切换。

## 实际执行路径

CLI/QQ → `runtime.open_application/open_chat/assemble_chat()` → `CharacterLoader.load("miru")` → `CharacterContextCompiler` → `compile_system()/runtime_sections()`。每轮 ContextBuilder 将角色编为一条 System，Core、时间、关系记忆、历史和可选短期状态仍通过原数据路径注入。自定义运行时段落覆盖同名默认说明，不重复拼接。

角色状态继续按 character_id 和包指纹区分；Miru 不继承 Sherlock 的短期 Mood/Focus。长期 Markdown 仍按 Agent 共享，现有对话与记忆不删除、不自动搬迁，也不把先前角色的经历直接声称为当前角色亲历。已有会话可能仍包含过去的助手回复，纯角色测试可在本地 CLI 使用现有新会话命令；本次不自动改变用户当前会话。

## 适配与边界

保留 Miru 的温暖、有主见、口语化、具体关心和随消息调整深度的表达。相处从实际互动形成，不沿用原项目默认“很亲近”的关系阶段，也不预设恋爱。兴趣和日常素材不自动成为真实活动、作息或位置记录。

只使用现有四个默认生产工具：get_current_time、archival_memory_search、recall_search、recall_get。后台学习维护 human/persona/主题文件，主模型普通聊天不获写权限；没有复制原项目的屏幕观察、图片管线、承诺工具、主动消息或后台循环。Learner 不修改角色包，手动 Reflection 审批照旧。

## 阅读与验证

最小离线读取：`CharacterLoader().load("miru")`，再用 CharacterContextCompiler.compile() 查看 System。`tests/test_character_loader.py` 验证独立身份、工具名、段落无重复与预算；`tests/test_qq_integration.py` 对 Miru/Sherlock 参数化验证共享生产装配、CLI→QQ 重启、记忆、角色短期状态和默认工具。FakeProvider 只证明接线，不证明自然度提升。

配置和加载失败沿用现有显式错误，不回退成 Sherlock 或未知身份；路径、文件和上下文字节限制不变。本次开发角色切换不自动执行真实模型评测，后续付费调用仍按具体任务授权。
