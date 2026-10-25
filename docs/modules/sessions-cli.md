# 会话管理与 CLI

## 架构组成

`sessions.py::SessionManager` 管理持久 Agent 和当前 Conversation；`main.py` 负责输入、命令和展示。
默认每个数据库使用 Agent `heybuddy`，其名称和 system 提示词保存在 agents 表。
存储支持多个 Agent 的记录关系，V0.2 CLI 不引入 Agent 创建/选择命令。
一个 Agent 可有多个 Conversation，共享身份和 system 提示词，不共享聊天历史。

## 代码实际运行路径

1. `main()` 加载 Settings、配置安全日志，`asyncio.run(run_cli())` 打开数据库并显式迁移。
2. `run_cli()` 标记遗留 Run，`SessionManager(store)` 加载或创建默认 Agent，
   读取该 Agent 的 last_conversation；首次创建空会话，其余恢复上次选择。
3. `_run_session()` 装配官方 SDK、Provider、内置工具、Agent，以及 `sessions.context()`。
   CLI 和存储均不进入 Provider 接口。
4. `chat()` 展示当前 Agent/Conversation/Run 状态，读取输入。普通文本调用 Agent.run()。
   `/new` 创建空会话并保存选择；`/sessions` 列出当前 Agent 的会话并标记当前项；
   `/resume <id>` 校验归属、验证可重放历史后保存选择；`/status` 展示状态。
5. 切换成功后只替换 Runtime 的 ContextBuilder，不重写 Loop，不混入上个会话的历史。
   失败时保留旧选择；slash 命令不送给模型、不保存为聊天消息。
6. exit / quit / EOF 正常退出；Ctrl+C 取消当前 Run，SDK 和数据库由上下文管理器关闭。
   再启动使用同一数据库路径，即可恢复之前选择的会话。

## 异常与边界

未知命令和缺少参数显示用法；不存在或其他 Agent 的会话不能切换。
数据库启动失败不退回临时内存会话，返回退出码 1；配置错误返回 2。
当前 Run 失败显示明确状态并继续读取输入，工具不会因历史恢复而重执行。
中断状态有明确提示，完整记录可通过存储 API 查看；没有任务续跑、历史删除或跨会话记忆检索命令。
多进程同时打开同一数据库会被拒绝；这是单人本地应用的明确约束。

## 示例与验证

启动 `uv run personal-agent`，输入主题后退出；再次启动查看 `/status`，向模型追问前文。
`/new` 后上下文只含 system 和新输入；用 `/sessions` 找到旧 ID，`/resume <id>` 恢复。
验证命令：`uv run pytest tests/test_config_cli.py tests/test_persistent_runtime.py`，
其中 `test_cli_session_commands_switch_and_restore_without_polluting_messages` 覆盖完整切换路径。
