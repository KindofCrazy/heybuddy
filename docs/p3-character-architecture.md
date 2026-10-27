# P3 Character Persona & Behavior

## 范围与基准

初始 HEAD 为 `77674b61feb749b2050317df776d1ebf6af71bf8`，比任务中的参考 d6ac715 更新，包含完整 P2。P3 仅实现被动交互：本地 Character Package、同一 Main Agent 的情境化人格、短期状态和真实关系记忆利用；不主动发消息，不实现 AttentionEngine 或主动聊天调度。

## 包与参考

启用方式：`.env` 中显式设置 `AGENT_CHARACTER_ID=sherlock_bbc`；默认 none 保持 P2 普通模式。可选 AGENT_CHARACTER_ROOT 只指定可信本地目录。MAX_FILE_BYTES 默认 16384、MAX_CONTEXT_BYTES 默认 24576、CONTEXTUAL_BYTES 默认 6144，分别限制包文件、完整角色层与可选材料；最终仍服从整个 ContextBudget，不能靠角色预算扩大模型窗口。基础人格超限直接配置错误，关键内容不静默截断。

实际注入顺序：Host System → Character Identity/Behavior System → 去掉旧 persona 身份的 Core 数据 → Working Summary/当前时间 → 有界 Lore/短期状态参考 → P1/P2 可选检索 → 近期与当前 Run。角色始终是同一 Agent.run() 的条件，不新增角色 Loop 或逐轮额外模型。旧 persona.md 保留原位，只有角色启用时的注入视图不含它；human.md、MEMORY.md、MemoryFS 工具和 P2 权限不改。角色包不是 P2 Writer 目标。

包格式和实际读取路径见 [Character 模块](modules/character.md)。sherlock_bbc 的四份 Markdown 是原创适配，选择剧版中后期已开始珍视友谊、仍然自负与情感表达笨拙的方向。年龄约三十多岁是创作基线，不是推断演员年龄或声称剧集明确给定。用户不映射为 Watson；虚构 Lore 与真实共同经历分开。

背景核对采用剧集发行方的 [PBS 剧版简介](https://www.pbs.org/show/sherlock/)、[A Study in Pink](https://www.pbs.org/wgbh/masterpiece/shows/sherlock/episodes/s1-e1/)、[The Sign of Three](https://www.pbs.org/wgbh/masterpiece/shows/sherlock/episodes/s3-e2/)、[The Final Problem](https://www.pbs.org/wgbh/masterpiece/shows/sherlock/episodes/s4-e3/)。只取简要背景关系，不复制对白。人物的内在动机与 Companion 适配是此项目的设计解释，不是剧集事实清单。

情境化行为受 [Mischel & Shoda 的 CAPS 原论文](https://psychology.columbia.edu/sites/psychology.columbia.edu/files/2016-11/246.pdf) 启发：稳定人格可以通过不同情境下的不同反应体现。工程中用 Markdown Behavioral Signatures 表达，不编写心理规则引擎，也不宣称模拟完整心理学系统。评测参考 [InCharacter](https://aclanthology.org/2024.acl-long.102/) 的访谈方向和 [CharacterBench](https://arxiv.org/abs/2412.11912) 的多维一致性方向；后续离线样本只验证本地契约，不复制这些 Benchmark 或声称真实角色一致性已经得到证明。

人格和情绪状态是行为模拟，不意味着模型有真实情绪、意识或线下经历。BBC Sherlock 是开发参考人物，不意味着影视特有视觉、对白等内容可未经授权商业化分发。
