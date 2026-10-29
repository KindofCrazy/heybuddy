# 当前记忆系统与模型工具

## 角色开启后的记忆分工

开启角色只在自动注入视图中移除 persona.md，不关闭记忆系统。文件仍保存在磁盘上，关闭角色时恢复普通模式的 Core 视图；显式授权的 Core 工具接口也未删除。P2 不自动写 persona.md，也不能修改角色包。

| 内容 | 存储位置 | 如何使用 |
| --- | --- | --- |
| 稳定人格 | 角色包 soul.md | 装配时读取，每次主请求编译为系统正文 |
| 用户重要信息与偏好 | Agent 记忆目录 human.md | 每次请求重新编译并注入 |
| 记忆索引与重要原则 | Agent 记忆目录根 MEMORY.md | 每次请求注入，用于渐进发现主题 |
| 整理后的项目、人物与知识 | 主题 Markdown 及各层 MEMORY.md | 有界自动选取相关章节，或由模型用工具按需读取 |
| 过去实际对话与工具轨迹 | SQLite Run/Message | 当前会话有界回放；跨会话由检索器或 Recall 取证 |
| 对话片段与话题整理 | SQLite Episode/Thread | 后台整理，作为历史定位及上下文线索 |
| 当前会话压缩摘要 | SQLite context_summaries | 历史较长时接续当前会话，不充当原始证据或长期事实 |
| 模拟短期心情与关注 | SQLite 角色状态 | 有来源和有效期，作为可选背景，不替换人格 |

Markdown 是整理后长期知识的权威来源；SQLite 原始消息是历史对话的权威来源。两者互补，不将 Episode/Thread 或会话摘要变成另一套可编辑事实库。

记忆按 Agent 共享，不按 Conversation 分家；新会话不自动继承另一个会话的全部聊天记录。默认新数据位于源码外的用户数据目录，AGENT_MEMORY_ROOT 可显式指定；升级时可沿用已存在的数据库相邻旧记忆目录。实际路径以本机启动配置为准，不在文档中复制私人内容。

## 存储和更新的实际路径

1. 用户输入由 Agent/ContextBuilder 写入 SQLite；Run、完整内部消息、工具调用及结果按原事务契约保存。中断记录保留审计，不自动重放或重试历史操作。
2. 回答交付后，ChatService.after_reply() 通知既有维护运行时。达到空闲或积累条件时，EpisodeOrganizer 整理已完成来源，ThreadOrganizer 维护跨片段话题；新前台输入可以抢占维护工作。
3. P2 默认启用，但实际开关以启动配置为准。明确的记住、偏好、决定、更正等信号可进入 Priority 队列；普通内容通过 finalized Episode 的 Batch 进入学习。不是每句话立即写长期记忆，也不默认回头补学所有旧历史。
4. MemoryLearningService 读取有界真实来源，提取候选、核对精确引文、读取现有记忆、按目标文件整合；重复和不确定内容可以不写或留待审阅。目标主要是 human.md 和主题文件，不写 persona.md 或角色包。说“记住”是学习线索，不等于给主 Agent 的记忆工具授予写权限。
5. 发布复用 MemoryFS/index：检查 Agent 路径、Git 隐私、容量、expected_hash 及局部精确匹配，在短锁内原子替换文件，并维护必要索引。人工编辑发生冲突时不覆盖。文件与 SQLite 没有共同事务，部分成功保留回执，由有界核对处理，不能声称回滚成功。
6. 低频 Curator 对少量相关文件去重、整理及核对有证据的状态变化；不是定时全库重写。失败或真实冲突继续保护，部分结果不盲目重做。

手动 Reflection 仍是独立的提案、审阅、批准流程。P2 开启时旧 MemoryAutomation 暂停自动消费新 Run；P2 关闭后才使用兼容的 off/suggest/auto 配置。后台学习、手动 Reflection 与模型本轮工具授权不是同一回事。

普通聊天不依赖主模型编辑记忆：Learner 负责从实际来源提取和整合，Curator 负责低频维护，MemoryFS 负责最终校验和发布；模型写入工具只在可信本地维护命令显式授权时提供。Curator 默认启用，但到检查时间后还要满足累计更新或长文件条件，并受共享维护预算约束，不能由 enabled 推断已经执行过整理。主模型的只读记忆搜索也不负责更新画像或运行 Reflection。

## 检索与利用的实际路径

1. 每次请求，ContextBuilder 通过 memory.compile() 读取最新 Core；角色模式过滤 persona.md，保留 human.md 和根 MEMORY.md。不是启动时只读一次，也不把整个主题库全部放进去。
2. ContextRetriever 在有个人或历史依赖线索时尝试检索；普通通用问题走快速路径。MemoryLookup 沿实际索引构造有界目录，用词法匹配挑选少量文件/章节。没有向量或 Embedding 数据库。
3. 历史路径有界匹配 Thread/Episode，必要时补充完整原始 Run。候选不明确且配置允许时，Preflight 只选择已有候选，可能增加一次受限模型请求；失败保留确定性结果。
4. ContextBuilder 去重并按剩余预算放入相关主题、话题线索和历史。可选材料放不下可省略，不挤掉当前用户消息。索引、摘要和文件修改时间不等于事实已经确认。
5. 主模型需要补充细节时，调用 deferred_list/deferred_read 渐进读取；需要过去具体说法时，用 recall_search 搜索、recall_get 展开。Recall 基于 SQLite 字面 FTS/受限短查询，原始检索只开放当前 Agent 的 completed/failed 历史，不执行旧工具调用。
6. 模型用当前提供的材料自然回答。工具调用和结果保存为普通消息并进入后续轮次；自动注入的 Core、时间和检索引用不追加为新的 Journal 原始消息。

## 注册了哪些工具

共享 runtime.assemble_chat() 注册 12 个生产工具。普通 CLI 输入和 QQ 被动消息默认没有写权限，通常提供以下 6 个：

| 工具 | 用途 |
| --- | --- |
| get_current_time | 获取当前时间 |
| deferred_list | 分页查看记忆目录元数据 |
| deferred_read | 分页读取主题或索引 |
| deferred_index_check | 只读检查索引问题 |
| recall_search | 搜索过去原始对话 |
| recall_get | 展开命中消息附近的历史 |

额外 6 个按本轮授权过滤：

| 工具 | 所需权限 | 用途 |
| --- | --- | --- |
| memory_read | memory.read | 重新读取 Core 块及摘要 |
| memory_edit | memory.update | 局部编辑 Core |
| memory_update | memory.update | 完整替换 Core |
| deferred_create | deferred.update | 创建主题并维护索引 |
| deferred_edit | deferred.update | 局部编辑主题或索引 |
| deferred_index_repair | deferred.update | 修复允许自动维护的索引条目 |

CLI `/memory read <请求>` 仅在本 Run 增加 memory.read，共 7 个工具；`/memory edit <请求>` 增加 memory.read、memory.update、deferred.update，共 12 个。下一次普通输入不继承授权。QQ 入口明确传入空权限集合。Reflection、Learner、Curator、Episode/Thread 和角色状态服务是宿主内部流程，不作为主模型工具注册。add_numbers 只保留在兼容的离线 Harness/评测注册表中，生产装配显式禁用。

## 哪些信息实际发送给模型

- messages 包含角色系统正文、过滤后的 Core、可选会话摘要、运行时时间、有效角色状态/来源、预算允许的检索结果、近期历史及当前 Run。
- 工具名称、中文描述和参数 JSON Schema 由 ToolRegistry.definitions(permissions=...) 过滤后，通过 Provider 的独立 tools 字段发送。它们不一定出现在单独导出的系统正文里，但计入总上下文预算。
- Agent 最后一轮不提供工具定义，要求结束回答；因此“默认 6 个”不表示每个请求都固定有 6 个。
- 模型返回调用后，注册表仍检查未知工具、当前权限和严格参数，随后顺序执行。工具文字不是权限授予机制。

## 本轮提示词与时间调整

角色共用说明新增【记忆是什么】与【什么时候检索】，分别解释材料类型和检索时机，不重复工具参数，也不恢复已删除的审计 JSON 或【真实与诚实】。

core.temporal.temporal_context() 发送给模型的 current_time 和 previous_interaction_at 使用本地 YYYY-MM-DD HH:MM:SS，不显示时区或单独 timezone 字段；内部转换和间隔计算仍使用带时区 datetime，跨午夜保持准确。本次修改针对自动注入的时间显示，不修改 SQLite 时间记录或独立时间工具的返回契约。

源码入口：`runtime.py::assemble_chat()`、`sessions.py::SessionManager.context()`、`core/context.py::ContextBuilder.prepare()`、`retrieval.py::ContextRetriever`、`memory/retrieval.py::MemoryLookup`、`learning/service.py::MemoryLearningService`、`tools/registry.py::ToolRegistry`。模块细节见 [Memory](modules/memory.md)、[Learning](modules/learning.md)、[Retrieval](modules/retrieval.md)、[Recall](modules/recall.md) 和 [工具](modules/tools.md)。

离线验证覆盖 `tests/test_continuous.py` 的跨午夜显示、`test_character_context.py` 的记忆说明、`test_memory_runtime.py` 的 Core 可见性、`test_memory_retrieval.py` 的自适应检索，以及 Learning/工具注册与 QQ 集成测试。本说明描述代码默认值和能力，不读取本机 .env 或私人记忆来推断实际开关。
