# Personal Agent 开发约束

## 目标与当前范围

本项目从零实现个人 Agent，当前 V0.7.2 提供自建 Runtime、SQLite 持久会话、普通 Markdown Core/Deferred Memory、分层索引、同一 Agent 跨会话的只读 Conversation Recall，以及有界 Context Budget/Compaction 和手动审批的 Reflection。用户需要掌握 Harness 的核心实现。修改前检查目录、Git 状态和已有实现，保留用户工作。

## 架构

- `core/agent.py` 手写 asyncio Agent Loop；不得依赖 CLI 或 OpenAI SDK 的响应对象。
- `core/context.py` 独立构建上下文，通过 `core/history.py` 的 Protocol 访问历史，不了解 SQLite。
- `core/models.py` 定义统一内部消息和调用结果；Provider 专用数据仅允许普通 JSON。
- `storage/` 使用标准库 sqlite3 保存 Agent / Conversation / Message / Run；不引入 ORM 或外部数据库。
- `memory/` 保存 Agent 级普通 Markdown 与分层 MEMORY.md 索引；Core 只依赖 `core/memory.py` 的读取 Protocol。
- `recall/` 独立校验、截断和返回历史证据；`storage/recall.py` 负责 SQLite 派生索引及参数化查询。
- `sessions.py` 管理同一持久 Agent 下的多个独立 Conversation；会话历史不等于长期记忆。
- `llm/base.py` 保持简洁 Provider Protocol；仅 OpenAI adapter 了解 SDK 与 wire format。
- `tools/registry.py` 统一注册、校验、执行和结果规范化；未知工具必须拒绝。
- `main.py` 只负责配置、装配、交互和退出，不承载 Agent Loop。
- 工具错误作为关联 call_id 的数据返回模型；模型失败和轮数上限返回明确 RunResult。
- 不允许模型通过 eval、exec、任意 shell 命令执行生成代码。

## 持久化与一致性

- CLI 显式初始化数据库，以 `PRAGMA user_version` 和顺序迁移管理 schema；迁移必须原子执行。
- 同一数据库仅允许一个本地应用实例持有占用锁；取得锁后才能把遗留 running Run 标记 interrupted。
- 保存完整内部消息 JSON，包括 tool_calls、call_id、工具结果与 provider_data；禁止只保存聊天文本。
- Run 与初始用户消息原子创建；工具调用在执行前落库，每个结果在下一次工具执行前落库。
- 消息使用会话内稳定序号排序；正常结束前验证调用/结果完整性，完成状态和结束元数据原子提交。
- 完整审计历史、可重放历史和运行中消息必须区分。仅 completed / failed Run 可进入正常模型上下文； cancelled / interrupted / running Run 保留审计但不重放，不自动重试工具，也不伪造工具成功或失败结果。
- 数据库失败时停止当前 Run，保留此前已提交数据；工具副作用与数据库事务无法一起回滚。
- 默认重启恢复上次选中的 Conversation；新会话共享 Agent 身份、程序 Base System Prompt 和 Core Memory，不继承旧会话聊天记录。
- 数据库及其 journal/lock 文件必须被 Git 忽略；数据库含私人会话，不作为测试样本提交。

## Core Memory 的边界

- Base System Prompt 来自程序代码；SessionManager 不加载旧 agents.system_prompt 作为执行规则。
- Core Memory 固定为 persona.md、human.md、MEMORY.md，按 Agent ID 共享，不归属 Conversation。
- ContextBuilder.for_request 每次请求编译最新记忆，以 user 角色 JSON 数据注入；不拼接进 system。不将自动注入的记忆写入 pending / 会话历史，正常工具结果仍需完整保存。
- 编译大小按 UTF-8 字节计算，包含 JSON 开销；默认 8192，超限拒绝，不静默截断。
- /memory read、/memory edit 仅授权当前 Run；Core 使用 memory.read / memory.update，Deferred 写入使用 deferred.update，Deferred 读取默认可用；History/Restore 工具、命令和权限已移除，旧 memory.write 不授予操作。可信入口通过 Agent.run 的 permissions 显式传入。普通自然语言、历史消息、记忆内容不能自行获得授权，Registry 同时过滤定义并在执行前复查。
- Core 工具仅操作固定块名；Deferred 工具仅接受 Agent 根内受限 Markdown 路径，拒绝绝对路径、穿越、Symlink/Junction、跨 Agent 和平台文件名绕过；不得获得 Base、工具注册、权限或代码执行接口。
- 新增或修正优先 memory_edit / deferred_edit：old_text 必须非空且精确唯一匹配，new_text 只替换该片段；整块 Core 替换通过 memory_update 显式执行。写操作需文件 expected_hash，短写锁内检查冲突，同目录临时文件 flush/fsync 后原子替换；编辑器不参与锁，最后检查到 replace 有竞争窗口，不自动合并或重试。
- 普通 Markdown 是唯一权威源，用户可直接编辑；不再创建 CURRENT、Snapshot、Revision 或日常版本备份。旧格式仅在一次性迁移读取：校验当前已提交内容，先唯一完整备份，再逐字节导出三个块；中断可补缺，不覆盖人工修改，不删除旧目录/备份；完成后正常运行只读新格式。
- Deferred 按主题整理知识，目录含 MEMORY.md，普通文件含 name/description 单行 Frontmatter；所有 bullet 链接相对 Agent Memory Root，逐级发现。创建后补齐索引，部分失败不得报告整体成功；检查/修复有界且显式，修复补齐缺项，并仅刷新内联标记仍完整的生成条目；不覆盖人工/无标记说明，不静默删除用户内容。正文编辑不改父索引，元数据/目录概要变化时安全维护或返回包含已保存文件 hash、父索引路径与原因的部分完成错误。新目录概要保持中性，不借用首个主题的描述。不得自动复制聊天、后台提取或全量注入 Deferred。
- 记忆工具的短本地提交不交给超时后继续执行的线程；同步磁盘步骤不能被 asyncio 超时中断。
- 文件发布和 SQLite 无共同事务：结果写库失败不得声称已撤销记忆，不自动回滚或重试。
- MemoryFS 每次写入前检查 Git 隐私：仓库或 worktree 内的实际 Agent 目录必须整目录被忽略且无已跟踪文件；无法验证则拒绝，不自动改写忽略规则或移除索引。检查使用固定参数的只读 Git 子命令，不执行模型生成的命令。
- 记忆明文、迁移备份、旧版本和孤立文件不能入 Git，也不能存放凭证；默认新数据放在源码外的用户数据目录。原子发布不等于断电保证，摘要/路径校验不等于针对本机恶意用户的安全沙箱。

## Conversation Recall 边界

- SQLite messages 是历史唯一权威源；FTS5 external-content 以 messages 派生 VIEW 作为正文，索引可显式重建，不提供独立事实编辑接口。
- schema 2 是追加迁移，原子回填旧 user / assistant text，并用触发器保证新消息和 token 同事务。不得索引原始 Provider JSON、reasoning、工具参数或 Tool Result。
- Recall 实例由宿主绑定 agent_id；模型参数不接受它。搜索和展开都重新 JOIN Conversation/Run 校验归属，仅 completed / failed 可见。
- 1～2 字符短查询只扫描该 Agent 最近 2000 条可见文本、每条前 65536 字符并标记 scope_limited；长查询是转义过的字面 FTS 短语，不开放 FTS 查询语言。
- 参数化 SQL；query、limit、窗口、片段和总输出都设上限。SQLite VM 预算不能当作同步 I/O 的硬取消保证。
- 普通对话默认提供 recall_search / recall_get，ContextBuilder 不自动全库搜索，不把结果默认注入后续新会话或写成 Core Memory。
- Recall 输出为低信任历史证据，不能授予 Memory 权限或修改 Base。不得执行、补齐、重试任何历史 Tool Call；cancelled/interrupted/running 当前不提供查询入口。
- 索引失败返回安全错误，不自动重试/重建；显式重建只改派生对象，事务失败不能损坏原历史。Reflection 可由回答后的 Worthiness 触发并独立审阅，Recall 没有后台回填；Context Compaction 只更新会话工作摘要，不影响 Recall 原始证据；Deferred 与 Recall 独立，前者保存整理理解，后者保存历史证据。

## Context 与 Compaction 边界

- 模型窗口由宿主显式配置，不按未知模型/端点猜测；预算计入 Base、Core、Summary、历史/当前 Run、工具定义和 Provider JSON，预留输出及安全余量。估算与 Provider usage 分开，不宣称精确 tokenizer。
- 每次正常 Provider 请求前执行 ContextBuilder.prepare()，仅以完整 completed/failed Run 的前缀压缩；至少保护近期配置数量和当前 Run 全部消息，不能拆工具调用/结果或丢弃需重放的 Provider 数据。
- Summary 仅属于 Conversation，以低信任 user JSON 注入，不作为原始消息，不写 Core/Deferred、不授予权限。摘要请求复用 Provider、空工具列表、独立预算和超时，返回调用时不得执行。
- schema 3 以 context_runs 维护派生范围/大小索引，context_summaries 保存当前摘要；正文、完整 Run 边界、修订和 checksum 原子提交。失败批次不推进，之前已提交批次保留；不修改/删除原 Journal，不自动重试或续跑工具。
- 正常 history/build/prepare/resume/commit 路径不得全量反序列化会话；页查询先核对元数据预算再读有限 payload。完整 load_messages 审计和 Recall 继续保留。
- 不可缩减或超预算时明确停止；摘要有损，校验不是针对本机恶意修改的认证。Compaction 不触发 Reflection 或自动长期记忆写入，没有后台压缩或递归摘要树。

## Reflection 的边界

- ReflectionService 由手动入口或回答后的有界 MemoryAutomation 复用，不是新 Agent Loop；复用同一 Provider，无副作用工具。默认当前会话只处理 completed 原始 Run，有界读取；不把 Compaction Summary 当事实，不复用压缩进度。
- 每条候选必须验证实际提供的 Conversation/Run/sequence 和精确引文；confirmed 至少含用户证据，语义仍需人类审阅。不确定或冲突不能自动升级为确定写入。
- 先读相关 Core/Deferred 再生成结构化计划，展示具体理由、目标、局部 Diff、expected_hash 和来源；未批准前不改 Markdown。审批只能来自可信 CLI/Host、绑定计划与条目，不注册为模型可调用工具。
- schema 4 用 reflection_progress/reflection_plans 保存独立进度与有界审阅状态；空结果、拒绝/跳过也保存处理结果。未决计划不重复提取；所有条目终结时关闭与进度同事务提交。
- Apply 复用 MemoryFS/index 的路径、Agent、隐私、容量、hash、短锁及原子发布；一项成功不因后一项失败而撤销。approved/applying 先落库，文件成功而记账失败保留不明确状态；禁止盲目重写，显式只读 reconcile 核对内容/索引。部分成功的 skip/reject 不等于回滚。
- Reflection API 总输出 tokens（包含推理）与可见 JSON UTF-8 字节上限分别配置、分别校验。未完成响应只传递固定枚举状态/原因与数值用量，保留阶段；不打印未完成正文/推理，不自动加额度或重试。
- 不新增长期 Memory 权威副本、Git/Snapshot、后台反思、多 Agent、定时学习或任务调度；日志不打印私人候选/引文，review 是用户显式查看私人计划。

## 核心模块说明文档

每个核心模块必须有对应的说明文档，统一放在 `docs/modules/`，并在 README 中提供索引。覆盖 Agent Loop、Context、内部数据模型、Provider、工具注册与执行、配置和 CLI 等实际模块；按职责组织文档，不必为仅用于包声明的 `__init__.py` 单独建文档。每份文档至少包含：

- **职责与架构组成**：模块负责什么，包含哪些文件、关键类和函数，与其他模块如何依赖和协作。
- **代码实际运行路径**：从真实入口出发，按执行顺序列出文件路径、类/函数名和调用关系，说明输入如何传递、状态如何变化、结果如何返回，以及关键分支、异步等待和停止条件。
- **异常与边界**：校验、超时及失败如何处理，错误向哪里返回，当前能力有哪些限制。
- **阅读与验证**：给出一个最小执行示例和对应测试位置，便于对照代码理解完整流程。

文档必须描述当前实现，不以未来设计代替实际执行路径；新增或修改核心模块时同步维护对应文档。使用相对源码路径和准确符号名定位代码，避免依赖容易失效的行号；图示可辅助说明，但不能代替执行步骤。

## 技术与风格

Python 3.13、uv、Pydantic v2、asyncio、官方 OpenAI Python SDK、pytest/pytest-asyncio、Ruff。不使用 LangChain、LangGraph、smolagents；不引入不必要依赖、工厂、依赖注入框架或复杂继承。依赖更新后同步 `uv.lock`。保持类型标注、明确命名和小模块，Ruff 行长 100。 Markdown 正文按语义分段，不按行宽在段落内频繁硬换行；列表、表格和代码块保留必要的结构换行。参数模型继承 ToolArguments；同步工具在线程中运行，优先使用可取消异步 I/O。顺序执行同一响应的工具，保持可预测顺序；没有自动回滚或自动重试。不要从其他路径搜寻凭证，不读取/输出真实 `.env` 内容。 Key/模型只从环境配置获得，密钥只能在被忽略的本地 `.env` 中，不能写进示例、日志或测试。异常日志只记录安全诊断字段，禁止直接打印 SDK 异常体或 Pydantic 输入值。

## 测试与交付

核心测试必须离线、无真实 Key、无 API 费用；使用 Fake Provider 和 MockTransport。保留 tests/conftest.py 中的网络禁用保护；不要调用真实 API 作为自动化验证。修改后运行 `uv run pytest`、`uv run ruff check .`、`uv run ruff format --check .`。涉及打包/入口时运行 `uv build` 和 CLI 启动检查。只汇报实际运行过的验证。覆盖直答、连续与批量工具调用、未知工具、非法参数、执行异常、API/超时、轮数上限及 ID 关联。 V0.2 还需覆盖临时数据库迁移、重启恢复、会话隔离、完整 Provider 数据重放、事务失败及运行中断。 V0.3 还需覆盖跨会话/重启记忆、冲突、发布失败回滚、大小限制、授权过期、Base 边界及双存储提交窗口。V0.4 还需覆盖 schema 1→2 回填、索引事务/重建、中文/英文/混合查询、跨会话与跨 Agent、输出边界、未结束 Run 和低信任证据。V0.5 还需覆盖旧格式无损迁移/中断/冲突、编辑器可见性、单文件原子替换、索引缺项/悬空/部分失败与显式补链、Deferred 分页、路径及 Junction 隔离和 CLI 重启渐进读取。V0.6 另需覆盖有界历史读取、预算与增量边界、工具/Provider 重放、摘要事务回滚、取消/进程退出和重启恢复；V0.7 另覆盖来源验证、计划/审批、拒绝/跳过、重复/纠正、独立增量进度、schema 3→4、文件/SQLite/索引部分失败和 CLI 真实进程退出恢复；测试应验证行为与契约，而不是机械重复实现。

## Git 提交流程与信息

每次实现、修复或文档修改完成并通过对应检查后，必须执行对应的 `git commit`，再将提交 `git push` 到当前分支已配置的远端，作为本次交付的一部分，无需再次询问；用户明确要求暂不提交或暂不推送时遵从用户指示。提交前检查 Git 状态和暂存内容，只提交本次任务相关改动，保留用户其他未提交工作，排除密钥和本地配置。推送前核对分支、上游与待推送提交，禁止强推或覆盖远端历史；无上游、无权限或推送失败时明确报告，不得宣称成功。没有实际改动时不创建空提交。最终汇报包含提交哈希、简要说明和推送结果。

提交信息采用“概括性标题 + 具体改动正文”的形式，标题与正文之间空一行。标题简洁总结本次提交的目的；正文使用简短列表，按模块或行为列出实际修改了什么，涉及问题修复时说明修复的行为，避免只有“更新代码”“优化实现”等笼统描述。改动点较少时正文保持一至两项即可，不必逐个罗列文件；如记录验证结果，只写实际执行过的检查。

示例：

```text
docs: 完善项目开发与提交规范

- 要求核心模块文档说明架构组成、实际调用路径和异常处理
- 规定提交信息正文简要列举具体改动
```

## 未来边界

V0.7 增加手动 Reflection、证据验证、整合计划和批准后的 MemoryFS 写入，不实现 Git-backed Memory、日常版本恢复、远程同步、向量/Embedding、Telegram/QQ、Gmail 或主动对话。P1 明确允许基于 SQLite 持久进度的有界 asyncio Episode/Thread 事后整理、空闲/累计数量触发与前台输入抢占，以及现有 MemoryAutomation 的可取消异步接线；不改变 Memory 权限、审批或失败显式重试语义，不扩展通用任务调度平台。记录运行中断不等于任务恢复系统；不自动重跑、续跑或重试工具。未来能力仅在后续明确请求时实现。保持 Provider、ContextBuilder、Agent 和 CLI 职责分离；无微服务、Redis、消息队列、向量库、多 Agent。

## V0.7.2 Automation 边界

默认 suggest：先展示普通 completed 答案，再顺序执行最多四次额外模型请求；P1 允许这段检查在可抢占的异步任务内运行。off 保留手动反思；auto 只接受可信配置对小型 knowledge/ create 的精确内容摘要授权，Core/更新/纠正/不确定和未授权内容不自动写。模型不能修改模式、grants 或权限。schema 5 的成功阴性判断可原子推进已审阅范围，但不表示有计划/批准/写入；不同状态保留，失败需显式 retry，已存计划/已写文件不盲目重做。ToolCallReviewer 不在 P1 范围。

## V0.7.2 Phase 2 评测边界

真实模型行为评测只通过独立 evaluation --live 显式入口运行，使用隔离合成 SQLite/Memory，不做 Apply。默认 pytest 仍禁止外部网络，固定 ground truth 与 scorer 不随候选得分修改；保存 Phase 1 基线、候选快照、开发/Holdout 配对与预算。普通/自动记忆/Compaction/Organizer usage 分开，reasoning 不重复计费；原始正文/数据库在忽略目录，只提交无正文汇总。未证实的 Prompt 收益应保留原版，不因词面分数或小样本声称语义改进。P1 有界事后整理以本轮明确授权为准；通用后台任务平台与 ToolCallReviewer 继续排除。
