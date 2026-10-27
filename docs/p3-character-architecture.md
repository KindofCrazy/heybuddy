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

## Character State 生命周期

实际状态与存储链路见 [State 模块](modules/character-state.md)。schema 12 追加 character_runs、character_states、character_state_sources：Host 标签记录实际角色归属，状态只保存一个当前有界模拟值，来源回执避免重付费。角色与包 hash 隔离；旧未标记 Run 不被默认为 Sherlock 经历。CharacterContextCompiler 读取状态视图，而不是把状态写回 Character Core、persona.md 或 P2 Memory。

即时反应由 Main Agent 在 Recent Runs 中自然延续；后台只针对新/修订 Episode 的本角色真实来源，在 Idle、速率与共享预算允许时作一次建议。默认 900 秒最小间隔，一小时 Mood 归于中性，三小时 Focus 失效；从所引事件时间计算，旧反例不能因晚处理被重新激活。no-op、失败、取消和处理完成分别保存，状态和 completed 回执原子提交，CAS 拒绝晚到结果。

状态模型没有好感度、亲密度分数、情绪向量或心理需求系统；输出 reason 只是有界可观察事件说明，不保存内部独白。包内容变化会使旧状态失效。/character status|run|retry 提供开发者入口，没有角色切换 UI；关闭 State 不影响角色核心，也不发送主动消息。

状态请求单独计量，默认最多一条维护请求/轮、24576 输入估算 tokens、2048 输出 tokens、4096 JSON 字节和20秒；连同 Episode/Thread/Learning/Curator 共用维护轮上限。P2 获得完整流水线预算轮时 P3 等待，角色状态不能使自动记忆长期饥饿。无新事件或来源已过期不付费判断。默认角色 none 下没有状态请求。

## Behavioral Signatures 与 Implicit Appraisal

behavior.md 的十二组情境签名分别覆盖证据挑战、失败与关心、无聊日常、科研新奇、代码/工具证据、自身错误、成果认可、熟悉用户的交流偏好、严肃简洁、不确定模式、Canon 与真实关系边界、任务结束与沉默。每组包含情境、角色解释、反应倾向、可观察行为及调节条件，是 LLM 在当前证据中采用的倾向，不是 Python if/else 人格规则引擎。

examples.md 提供十四段原创示例，含反证、承认错误、工具真实失败、现实感官边界和经过真实检索才成立的共同经历。每轮最多选择两段；示例的 Context 是虚构演示条件，不能当作当前用户实际历史。Compiler 对示例做有界词面选择，对行为签名整体编译；人格因此在正常决策前影响关注点，不另做通用答案后的风格改写。

Agent.run/ContextBuilder.prepare/Provider.generate/ToolRegistry/Journal 路径不变。没有额外 Appraisal、内部独白、认知 JSON、答案重写或 Persona Critic 调用。tests/test_character_behavior.py 的情境样本证明每次主请求实际携带完整签名和少量示例，且无额外请求；恶意角色文本提出未知工具仍被 Registry 拒绝。这些是工程契约验证，不代表 FakeProvider 已证明自然语言中的角色品质。
