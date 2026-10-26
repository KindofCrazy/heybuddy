# 会话管理与 CLI

## 架构组成

`sessions.py::SessionManager` 管理持久 Agent 和当前 Conversation；`main.py` 负责输入、命令和展示。默认每个数据库使用 Agent `heybuddy`，其名称和兼容 system_prompt 字段保存在 agents 表。存储支持多个 Agent 的记录关系，当前 CLI 不引入 Agent 创建/选择命令。一个 Agent 可有多个 Conversation，共享身份和 Core Memory，不共享聊天历史。Base 来自代码，不加载旧 agents.system_prompt。

## 代码实际运行路径

1. `main()` 加载 Settings、配置安全日志，`asyncio.run(run_cli())` 打开数据库并显式迁移。
2. `run_cli()` 标记遗留 Run，`SessionManager(store, budget=settings.context_budget())` 加载或创建默认 Agent，读取该 Agent 的 last_conversation；首次创建空会话，其余恢复上次选择。
3. `run_cli()` 按 Agent ID 初始化或迁移 MemoryFS，绑定 sessions.memory，再由 `_run_session()` 装配官方 SDK、Provider、内置工具、Agent，以及 `sessions.context()`。 CLI 和存储均不进入 Provider 接口。
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

`_run_session()` 在创建 Runtime 前把 Recall 绑定到 sessions.agent.id 并注册两项只读工具。/new 只替换 ContextBuilder，Recall 继续查询同一持久 Agent 下的历史；不把其他会话复制到当前上下文。用户直接问“我们之前为什么决定先实现 Memory？”即可由模型选择搜索→展开→回答；没有增加 /recall 管理命令。schema 2 初始化回填在接收输入前完成，坏索引不自动修复，显式修复 API 见[Recall 模块](recall.md)。

## V0.6 上下文与诊断

run_cli() 用 Settings.context_budget() 构造 SessionManager；context() 将同一预算及当前 Conversation 的 adapter 传给 ContextBuilder。resume() 不再调用全量 load_messages()，只验证 Agent 归属和已有摘要检查点；prepare() 在真正发送前读取/校验完整 Run 页。/new 不继承其他会话的摘要。status() 用 run_statistics() SQL 统计，不反序列化全部 RunResult。

`/context status` 在 chat() 中直接调用 diagnostics()，不创建 Run、不调用模型，显示显式窗口、输出预留、输入上限、本地估算、服务端 usage（可用时）、摘要边界/修订/大小、当前加载页规模和最近压缩结果；不显示摘要、消息或 reasoning 正文。首次请求前 estimated_tokens 为空表示尚无实际准备记录，不是精确零 token。bad checkpoint/预算或数据库错误返回安全诊断。

启动必须配置 AGENT_CONTEXT_WINDOW，帮助入口仍不读配置。长会话达到水位或分页资源条件时先摘要再正常请求；当前 Run 太大或摘要失败返回明确状态，CLI 继续接收下一次用户请求，不自动重试工具。实际路径见[Compaction](compaction.md)，测试见 `tests/test_config_cli.py` 和 `tests/test_compaction.py`。
