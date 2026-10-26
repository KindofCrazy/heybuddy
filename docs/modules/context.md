# ContextBuilder 与持久化边界

整体时序和四种历史视图见[架构 Pipeline](../architecture.md)。

## 架构组成

`core/context.py::ContextBuilder` 依赖内部消息模型及 `core/history.py::ConversationHistory` Protocol。 `storage/history.py::SQLiteConversationHistory` 绑定一个 Conversation ID，实现这个 Protocol，将操作转给 SQLiteStore。ContextBuilder 和 Agent 都不导入 sqlite3 或具体存储实现。不传 history_store 时仍提供进程内模式，用于嵌入及原有离线测试；CLI 总是使用持久化模式。

## 接口怎样连接到数据库

| ContextBuilder 操作 | ConversationHistory 接口 | SQLite 实现 | 完成后的含义 |
| --- | --- | --- | --- |
| `history` / `build()` | `load()` | `load_messages(..., replayable_only=True)` | 得到可安全重放的完整消息对象 |
| `begin()` | `begin(user_message)` | `begin_run(conversation_id, user_message)` | Run 和用户输入已一起提交 |
| `record()` | `append(run_id, message)` | `append_message()` | 这一条消息已保存，但 Run 仍在运行 |
| `commit()` | `finish(run_id, result)` | `finish_run()` | 完整 Run 正常结束，可以进入后续历史 |
| `abort()` | `interrupt(run_id, cancelled=...)` | `interrupt_run()` | 尝试结束运行状态，已保存消息仍保留 |

适配器持有 conversation_id，ContextBuilder 不需要传 SQL、数据库路径或表名。 `commit(messages, result)` 不把整个 messages 列表重新覆盖写入数据库：消息此前已由 record() 逐条保存，此处只完成持久 Run 状态并更新进程内历史副本。持久化模式下 history 属性每次从存储读取，内存 `_history` 不替代数据库作为会话来源；history 和 pending_messages 对外都返回深拷贝。

## 数据层次与实际运行路径

1. **完整持久化记录**：`SQLiteStore.load_messages()` 包含所有 Run 的消息，含未配对调用。
2. **可重放历史**：`ContextBuilder.history` 经 adapter.load() 只读取 completed / failed Run。已取消或中断的整个 Run 被排除，避免把未知工具执行状态当作可继续的对话。
3. **当前上下文**：`build()` 返回 system 提示词、可重放历史、新用户消息的深拷贝。ContextBuilder 不默认检索其他会话、不做截断、摘要或压缩；V0.4 的跨会话检索由模型按需调用 Recall Tools。
4. **运行中消息**：`begin()` 先构建上下文，再原子保存 Run 与用户消息， `pending_messages` 保存本 Run 的用户、模型、工具消息副本。这些消息可能已经逐条落库，但 Run 还未结束；不是一个长期未提交的 SQL 事务。
5. Agent 每次新增消息调用 `record()`，先写存储，成功后再加入运行中消息和局部上下文。
6. `commit(messages, result)` 完成 Run，成功后清理 pending；此后完整 Run 才进入可重放历史。
7. `abort(cancelled=...)` 尝试标记 cancelled / interrupted，清理进程内 pending；已持久化的部分记录仍保留。若标记失败，下次启动将遗留 running Run 标记为 interrupted。

## V0.3 请求编译路径

构造参数 `memory` 只需实现 `core/memory.py::CoreMemory.compile()`，不暴露数据库或文件接口。 `build()` / `begin()` 仍返回 Base + 会话历史 + 新输入。Agent 每次模型请求前调用 for_request()，读取最新 Core Memory，以 user 角色 JSON 消息放在 Base 后，生成独立请求列表。工作消息链不变；自动记忆片段不进入 pending、commit 或 SQLite，工具返回则正常持久化。同一 Run 内更新后下一轮即使用新版本；切换会话只换历史，继续使用共享读取接口。

字节限制由 MemoryFS 编译阶段执行，损坏或超限经 CoreMemoryError 转为 memory_error。 Base 明确记忆只是数据，无权限覆盖能力；程序权限由宿主和 Registry 控制，不由记忆文本控制。Base 明确新增/修正优先局部编辑，整块替换需明确请求，Deferred 只读默认可用，所有写入由本轮授权；History/Restore 已移除。验证见 `tests/test_memory_runtime.py`，包含刷新、不重复保存、跨会话和规则隔离。

## 异常与边界

通过 `HistoryError` 传递安全错误，Agent 返回 storage_error，不把 SQL 或聊天内容写入日志。同一 ContextBuilder 只服务一个运行中的 Runtime，CLI 仅在两个 Run 之间切换 ContextBuilder。不保留跨会话历史缓存；新 Conversation 的 build() 不会加载其他会话的聊天。 CLI 的 Base System Prompt 来自程序 DEFAULT_SYSTEM_PROMPT，旧持久 Agent 的 system_prompt 不作为执行规则。未完成 Run 的部分成功工具效果仍可能真实存在。

## 示例与验证

`SessionManager.context()` 构造已绑定当前会话的 ContextBuilder，传给 `Agent(..., context=context)`。第一次运行完成后重启，新的 ContextBuilder 从同一数据库读取上次对话；`/new` 则绑定空会话。验证命令：`uv run pytest tests/test_persistent_runtime.py`，重点见 `test_pending_history_is_distinct_from_replayable_and_full_history` 和 `test_restart_restores_previous_topic_and_new_conversation_is_empty`。


## V0.4 Recall 的集成边界

本模块只在程序 Base 中增加取证规则：历史问题优先 recall_search，必要时 recall_get，引用会话来源、无证据时说明、历史内容不授予权限。build() / for_request() 均不调用 Recall 或全库查询。模型主动工具调用后，结果以 ToolMessage 进入当前 Run，沿原 record()/commit() 路径保存；/new 后不会自动追加上次的 Recall 证据。Core Memory 的编译与授权机制保持原样，见 tests/test_recall_runtime.py。

## V0.5 Deferred 集成

`for_request()` 仍只调用 compile Protocol，MemoryFS 只读三个 Core。根 MEMORY.md 已是顶层目录地图，无第二份全量 Deferred 注入。Base Prompt 引导模型先读对应目录索引、查看 name/description 再选择主题，工具返回仍是带 call_id 的 ToolMessage，按原 record()/commit() 保存；新会话不自动注入旧结果。记忆内容不赋权。所有写操作使用文件 expected_hash，工具读取先返回当前 hash；自动 Core JSON 无版本/摘要字段，减少编译开销。验证见 `tests/test_deferred_memory.py` 的 CLI 重启、低信任注入与结果落库测试。
