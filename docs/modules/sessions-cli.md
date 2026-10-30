# 会话管理与 CLI

`main.main()` 的渠道选择包含 `chat`、`qq`、`napcat`、`web`。`napcat` 调用 `channels/napcat/runner.py::run_napcat()`，与官方 QQ 一样可加 `--dashboard` 和 `--dashboard-port`，复用同一个 `runtime.open_application()`；`--discover-openid` 仍仅限官方 `qq`。NapCat 使用普通 QQ 号码绑定，不需要 OpenID 发现。入口及退出监督见 [NapCat](napcat.md)，测试见 `tests/test_napcat.py`。

P3 runtime.assemble_chat() 在装配前加载角色包并设置 SessionManager.character，Agent 使用原 sessions.context()。同一编译器跨 Conversation 复用，角色启用时绑定 Host Run 标签、来源归属和 State 有效视图；默认 none 无角色/状态请求。foreground() 先抢占维护任务，再处理 `/character status|run|retry`，只返回安全调试状态，不注册模型入口或主动消息。细节见 [Character](character.md)、[State](character-state.md)；测试见 tests/test_character_context.py、tests/test_character_state.py。

P2 `runtime.assemble_chat()` 在原 Provider/工具/Context 上装配 MemoryLearningService 与 MemoryCurator，传给唯一 OrganizationRuntime；ContextRetriever 同时绑定 MemoryFS 与受限只读 Preflight。输入依然先 foreground() 取消等待后台，再处理普通 Agent 或可信调试命令。`/memory learning status|run|retry|reconcile <job_id>|retry <job_id>`、`/memory curator status|run|retry` 不注册为模型写工具，不增加逐条审批流程；`/context status` 显示实际注入 Memory 数、预算和额外调用。debug 调用有共享额度及整轮超时，失败不打印候选/正文。P2 enabled 暂停旧 Automation 自动入口，旧计划仍能 /reflect 查看。tests/test_p2_cli.py 验证真实装配与只读命令，tests/test_p2_recovery.py 验证真正进程退出。

## P1 持续流

生产 CLI 装配 OrganizationRuntime，用 AsyncInput 读取终端；新输入先取消事后整理，所有数据库操作仍在事件循环线程。`/episode status|organize|list|show <id>` 是开发者入口；完整事务、空闲/数量触发与取消语义见 [Organization](organization.md)。

普通启动自动沿用 `client_state` 上次选择的 Conversation；没有选择时创建第一条流。旧 Conversation、Run、Summary 和 Reflection 不迁移归属，不合并、不删除，Recall 仍按 Agent 查询。`/new`、`/sessions`、`/resume` 保留为开发者历史管理入口，普通聊天无需调用。

`SessionManager.context()` 向 `ContextBuilder` 传递宿主时间回调。`for_request()` 每次请求用 `core/temporal.py::temporal_context()` 注入带本地 UTC offset 的当前时间、时区及距上个已终结 Run 开始时间的秒数；SQLite `last_interaction()` 只查一条元数据。时间信息以 user JSON 注入，参与预算，不写 Journal，也不授予权限。跨午夜不清空上下文。测试见 `tests/test_continuous.py`。

## 架构组成

`sessions.py::SessionManager` 管理持久 Agent 和当前 Conversation；`main.py` 负责输入、命令和展示。默认每个数据库使用 Agent `heybuddy`，其名称和兼容 system_prompt 字段保存在 agents 表。存储支持多个 Agent 的记录关系，当前 CLI 不引入 Agent 创建/选择命令。一个 Agent 可有多个 Conversation，共享身份和 Core Memory，不共享聊天历史。Base 来自代码，不加载旧 agents.system_prompt。

## 代码实际运行路径

1. `main()` 加载 Settings、配置安全日志，`asyncio.run(run_cli())` 打开数据库并显式迁移。
2. `run_cli()` 标记遗留 Run，`SessionManager(store, budget=settings.context_budget())` 加载或创建默认 Agent，读取该 Agent 的 last_conversation；首次创建空会话，其余恢复上次选择。
3. `run_cli()` 按 Agent ID 初始化或迁移 MemoryFS，绑定 sessions.memory，再由 open_chat() 持有官方 SDK 并调用 `runtime.assemble_chat()` 装配 Provider、内置工具、Agent，以及 `sessions.context()`。 CLI 和存储均不进入 Provider 接口。
4. `chat()` 展示当前 Agent/Conversation/Run 状态，读取输入。普通文本调用 Agent.run()；始终提供绑定当前 Agent 的 recall_search / recall_get，不需要记忆写入授权。 `/new` 创建空会话并保存选择；`/sessions` 列出当前 Agent 的会话并标记当前项； `/resume <id>` 校验归属和摘要检查点后保存选择，实际历史页在模型请求前有界验证；`/status` 展示状态。
5. 切换成功后只替换 Runtime 的 ContextBuilder，不重写 Loop，不混入上个会话的历史。失败时保留旧选择；会话管理命令不送给模型、不保存为聊天消息。
6. exit / quit / EOF 正常退出；Ctrl+C 取消当前 Run，SDK 和数据库由上下文管理器关闭。再启动使用同一数据库路径，即可恢复之前选择的会话。

## 记忆授权的实际路径

chat() 每次循环先设空权限。`/memory read <请求>` 授予本轮 memory.read；`/memory edit <请求>` 额外授予 memory.update 和 deferred.update，传给 Agent.run()。完整命令作为用户消息保存以供审计；下一次输入重置权限，历史、普通自然语言和 Memory 内容不能赋权。Deferred list/read/index_check 与 Recall 默认只读可用。`/memory restore` 已拒绝，不再暴露 History/Restore 工具或权限。

`/memory check` 直接调用 `index.inspect()`；`/memory repair` 是用户对本地补链操作的显式授权，直接调用 `index.repair()`，不通过模型、不创建 Run，也不延续到下一次输入。检查分页结果包含 next_offset，可由 deferred_index_check 继续查看；修复补齐缺项并安全刷新标记完整的生成条目，保留人工说明；仍有悬空/无效/重复链接或标记改写提示时返回 ok=false，失败时明确提示部分操作可能已发生。工具参数和示例见[MemoryFS](memory.md)。

main() 仅在运行期安装日志 handler，退出时恢复之前的日志配置并关闭 handler，避免多次嵌入调用复用已关闭的输出流。异常正文和私人路径不记录到日志。

## 异常与边界

未知命令和缺少参数显示用法；不存在或其他 Agent 的会话不能切换。数据库启动失败不退回临时内存会话，返回退出码 1；配置错误返回 2。当前 Run 失败显示明确状态并继续读取输入，工具不会因历史恢复而重执行。中断状态有明确提示，完整记录可通过存储 API 查看；没有任务续跑或历史删除；V0.4 不需要额外检索命令，普通输入即可由模型选择 Recall Tools。多进程同时打开同一数据库会被拒绝；这是单人本地应用的明确约束。

## 示例与验证

启动 `uv run personal-agent`，输入主题后退出；再次启动查看 `/status`，向模型追问前文。 `/new` 后上下文含程序 Base、共享 Core Memory 和新输入；用 `/sessions` 找到旧 ID，`/resume <id>` 恢复。验证命令：`uv run pytest tests/test_config_cli.py tests/test_persistent_runtime.py`，其中 `test_cli_session_commands_switch_and_restore_without_polluting_messages` 覆盖完整切换路径。


## V0.4 无额外命令的 Recall

`runtime.assemble_chat()` 在创建 Runtime 前把 Recall 绑定到 sessions.agent.id 并注册两项只读工具。/new 只替换 ContextBuilder，Recall 继续查询同一持久 Agent 下的历史；不把其他会话复制到当前上下文。用户直接问“我们之前为什么决定先实现 Memory？”即可由模型选择搜索→展开→回答；没有增加 /recall 管理命令。schema 2 初始化回填在接收输入前完成，坏索引不自动修复，显式修复 API 见[Recall 模块](recall.md)。

## V0.6 上下文与诊断

run_cli() 用 Settings.context_budget() 构造 SessionManager；context() 将同一预算及当前 Conversation 的 adapter 传给 ContextBuilder。resume() 不再调用全量 load_messages()，只验证 Agent 归属和已有摘要检查点；prepare() 在真正发送前读取/校验完整 Run 页。/new 不继承其他会话的摘要。status() 用 run_statistics() SQL 统计，不反序列化全部 RunResult。

`/context status` 在 chat() 中直接调用 diagnostics()，不创建 Run、不调用模型，显示显式窗口、输出预留、输入上限、本地估算、服务端 usage（可用时）、摘要边界/修订/大小、当前加载页规模和最近压缩结果；不显示摘要、消息或 reasoning 正文。首次请求前 estimated_tokens 为空表示尚无实际准备记录，不是精确零 token。bad checkpoint/预算或数据库错误返回安全诊断。

启动必须配置 AGENT_CONTEXT_WINDOW，帮助入口仍不读配置。长会话达到水位或分页资源条件时先摘要再正常请求；当前 Run 太大或摘要失败返回明确状态，CLI 继续接收下一次用户请求，不自动重试工具。实际路径见[Compaction](compaction.md)，测试见 `tests/test_config_cli.py` 和 `tests/test_compaction.py`。

## V0.7 手动 Reflection 命令

`runtime.assemble_chat()` 使用 ReflectionRepository(store, sessions.agent.id)、同一 MemoryFS/Provider、ContextBudget 和 Settings.reflection_policy() 装配 ReflectionService，传给 chat()。`/reflect` 命令在普通 Agent.run() 之外分派，不保存为聊天消息或创建新 Run；/resume 后始终使用当前 Conversation，长期 Markdown 仍按 Agent 共享。

- `/reflect`：处理当前会话一批尚未审阅的 completed Run，或返回已有待审阅计划。
- `/reflect status`：只显示独立边界、活动计划 ID 与逐项状态，无模型调用/消息正文。
- `/reflect review [plan_id]`：展示候选、来源、理由、目标、Diff、hash 与结果；已关闭计划也可按 ID 查询。
- `/reflect apply <plan_id> <item_id|all>`：可信显式批准；只执行未完成且适用的操作，review 候选不自动升级。
- `/reflect reject|skip <plan_id> <item_id|all>`：持久记录决定，不撤销已经发生的文件副作用。
- `/reflect reconcile <plan_id> <item_id>`：只读核对 applying/partial/uncertain 项，不重写文件；确认 hash 与索引后记账。

未完整提供审批参数只显示用法。ReflectionError/Memory 错误只显示安全代码；HistoryError 明确提示文件可能已完成、不自动重试。Provider await 取消时按原 CLI 退出语义处理，未保存的生成结果不会推进状态；已存计划跨重启可恢复。真实执行路径及离线子进程验证见[Reflection](reflection.md)和 tests/test_reflection.py。

V0.7.1 的 Reflection 模型失败会附加阶段及安全 ProviderDiagnostics（状态、固定停止原因、计数），不输出未完成正文或推理。看到 max_output_tokens 时应检查 API 总输出额度；可见 JSON 则独立受 AGENT_REFLECTION_RESPONSE_MAX_BYTES 约束，不自动加额度重试。


## V0.7.2 Phase 1 集成

V0.7.2 的 chat() 先展示 completed 答案，再调用 MemoryAutomation.run()；模式 off 不触发。/memory automation off|suggest|auto|status|run|retry 由可信 CLI 解析，不注册为工具，运行中模式只在当前进程有效。用户处理完计划后，下次 completed Run 或显式 run 继续积压；失败/cancel 不改变普通 Run 终态。
# Shared Runtime 接线

`main.run_cli()` 通过 `runtime.open_application()` 持有数据库、恢复会话并初始化 MemoryFS；`runtime.open_chat()` / `assemble_chat()` 统一装配 Provider 与 P1/P2/P3。`main._run_session()` 保留为已有会话的兼容包装入口。CLI 本地命令解析仍在 `main._chat()`，普通输入交给 `ChatService.run()`，展示后 `after_reply()` 恢复既有维护。完整调用路径见 [runtime.md](runtime.md)。

`main.main()` 接受可选的 `chat` / `qq` 位置参数，默认 CLI 兼容。`qq` 调用 `channels.qq.runner.run_qq()`，`qq --discover-openid` 为不初始化 Agent 的限时诊断。两种正常聊天模式共用数据库单进程锁，不能以两个进程同时连接同一数据库；QQ 中的 `/new` 等文本不进入 CLI 命令分支。参见 [qq.md](qq.md)。
# Observatory 入口

`SessionManager.context()` 为持久会话传入 `SQLiteRunTrace(self.store)`，关联的请求快照沿用原数据库锁和 Run ID，不新开 Owner，也不影响跨会话记忆或历史选择。调用路径见[模型调用审计](tracing.md)。

`main.py:main` 新增 `web` 和 QQ 专用 `--dashboard`，`--dashboard-port` 限定 1024–65535，默认 8765。`web` 不读取聊天输入，不调用 `Agent.run()`，通过原 Owner 锁保护独立实例；`qq --dashboard` 共享 `run_qq` 内的同一 `open_application()`。Ctrl+C/SIGTERM 沿原 Runtime 生命周期退出。详见 [dashboard.md](dashboard.md)。


## CLI 资源与命令

生产 run_cli() 和兼容 _run_session() 都将 open_chat 装配的同一个 ChatService 交给 chat(service)。CLI 拥有 console.AsyncInput，只在存在异步维护时创建 daemon 输入桥，并在退出 finally 关闭；open_chat 最终关闭维护资源。chat 仍接受分开的 Agent/服务参数供离线嵌入，此时它负责关闭自己组装的轻量 Service。cli_commands.MaintenanceCommands 处理 Episode/Thread/Learning/Curator/Character 命令与共享预算；Core Agent 不理解命令。测试见 test_p2_cli.py 的同实例/关闭归属和 test_organization_runtime.py 的 EOF/抢占。
# Proactive 可信命令

`main._chat()` 在普通 Agent.run 之前分派 `/proactive`，先沿用 foreground() 抢占维护；`MaintenanceCommands.proactive_command()` 提供 status/proposals/decisions 只读查询、显式 retry，以及受 MaintenanceBudget 约束的一次 run。命令不进入模型用户消息，不授予工具权限，也不进行发送；完整语法、执行路径、错误和离线验证见 [proactive.md](proactive.md)。

# P4.3 离线 Outbox 命令

原 `/proactive` 分派增加 outbox/attempts 只读分页、`simulate <id>` 和 `retry-compose <id>`。simulate 明确创建本地 FakeSender 和当前会话的模拟许可，不调用模型或真实 QQ/NapCat；retry-compose 在原维护预算内复核现有来源后重新生成，不能重发 uncertain 或 accepted。命令不写用户 Run。SessionManager.notice_factory 构造当前 Conversation 的只读 notices Protocol，默认排除模拟记录；测试 Harness 可显式开启并验证“还没做完”的接续。符号、错误与验证见 [outbox.md](outbox.md)。

## 共享会话切换

`main._chat()` 的 /new 与 /resume 统一调用 `ChatService.select_conversation()`，由共享 Runtime 同步切换 SessionManager 和 Agent.context。SessionManager.context 的时间查询绑定该 Context 的 Conversation ID，不跟随后续 SessionManager.current 变化。只读 SessionManager 仅加载已有会话，禁止 new，resume 不写 client_state；错误沿原 CLI HistoryError 路径展示。验证：`tests/test_config_cli.py`、`tests/test_review_refactoring.py`。

会话命令解析由 `cli_commands.session_command()` 负责，`main._chat()` 仅分派；该函数调用共享 Runtime 并沿用原安全错误展示。

# P4.4.1 明确提醒入口

`main._chat()` 在 Agent.run 前将 `/remind` 交给 `scheduler/cli.py::command()`，仅接受明确的带时区 at 或相对 in，以及 status/list/show/events/cancel/simulate。命令不会写用户 Run 或通过模型授予权限。目标 Conversation 在创建时固定，不随 SessionManager 当前会话变更；取消仍按当前 Host 的 Agent Scope 校验，模拟使用记录原会话的本地许可。没有真实发送保证，详细解析、状态和待执行用例见 [scheduler.md](scheduler.md)。

共享解析器还接受 Host 显式提供的 request_id、reference_at 与 source_scope，供 `scheduler/chat.py::reply()` 对已验证的 QQ/NapCat 私聊做受限创建；CLI 默认使用随机请求 ID、当前时钟和 local 来源。聊天 list/show/cancel 不调用 CLI 的 Agent 级管理分支，而在聊天入口额外限制来源和会话；不开放 status/events/simulate。
