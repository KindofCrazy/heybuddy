# ContextBuilder 与持久化边界

整体时序和四种历史视图见[架构 Pipeline](../architecture.md)。

## 架构组成

`core/context.py::ContextBuilder` 依赖内部消息模型及 `core/history.py::ConversationHistory` Protocol。
`storage/history.py::SQLiteConversationHistory` 绑定一个 Conversation ID，实现这个 Protocol，
将操作转给 SQLiteStore。ContextBuilder 和 Agent 都不导入 sqlite3 或具体存储实现。
不传 history_store 时仍提供进程内模式，用于嵌入及原有离线测试；CLI 总是使用持久化模式。

## 接口怎样连接到数据库

| ContextBuilder 操作 | ConversationHistory 接口 | SQLite 实现 | 完成后的含义 |
| --- | --- | --- | --- |
| `history` / `build()` | `load()` | `load_messages(..., replayable_only=True)` | 得到可安全重放的完整消息对象 |
| `begin()` | `begin(user_message)` | `begin_run(conversation_id, user_message)` | Run 和用户输入已一起提交 |
| `record()` | `append(run_id, message)` | `append_message()` | 这一条消息已保存，但 Run 仍在运行 |
| `commit()` | `finish(run_id, result)` | `finish_run()` | 完整 Run 正常结束，可以进入后续历史 |
| `abort()` | `interrupt(run_id, cancelled=...)` | `interrupt_run()` | 尝试结束运行状态，已保存消息仍保留 |

适配器持有 conversation_id，ContextBuilder 不需要传 SQL、数据库路径或表名。
`commit(messages, result)` 不把整个 messages 列表重新覆盖写入数据库：消息此前已由 record()
逐条保存，此处只完成持久 Run 状态并更新进程内历史副本。持久化模式下 history 属性每次从存储读取，
内存 `_history` 不替代数据库作为会话来源；history 和 pending_messages 对外都返回深拷贝。

## 数据层次与实际运行路径

1. **完整持久化记录**：`SQLiteStore.load_messages()` 包含所有 Run 的消息，含未配对调用。
2. **可重放历史**：`ContextBuilder.history` 经 adapter.load() 只读取 completed / failed Run。
   已取消或中断的整个 Run 被排除，避免把未知工具执行状态当作可继续的对话。
3. **当前上下文**：`build()` 返回 system 提示词、可重放历史、新用户消息的深拷贝。
   V0.2 不做检索、截断、摘要或压缩。
4. **运行中消息**：`begin()` 先构建上下文，再原子保存 Run 与用户消息，
   `pending_messages` 保存本 Run 的用户、模型、工具消息副本。
   这些消息可能已经逐条落库，但 Run 还未结束；不是一个长期未提交的 SQL 事务。
5. Agent 每次新增消息调用 `record()`，先写存储，成功后再加入运行中消息和局部上下文。
6. `commit(messages, result)` 完成 Run，成功后清理 pending；此后完整 Run 才进入可重放历史。
7. `abort(cancelled=...)` 尝试标记 cancelled / interrupted，清理进程内 pending；
   已持久化的部分记录仍保留。若标记失败，下次启动将遗留 running Run 标记为 interrupted。

## 异常与边界

通过 `HistoryError` 传递安全错误，Agent 返回 storage_error，不把 SQL 或聊天内容写入日志。
同一 ContextBuilder 只服务一个运行中的 Runtime，CLI 仅在两个 Run 之间切换 ContextBuilder。
不保留跨会话历史缓存；新 Conversation 的 build() 不会加载其他会话的聊天。
system 提示词来自持久 Agent，不是长期记忆。未完成 Run 的部分成功工具效果仍可能真实存在。

## 示例与验证

`SessionManager.context()` 构造已绑定当前会话的 ContextBuilder，传给 `Agent(..., context=context)`。
第一次运行完成后重启，新的 ContextBuilder 从同一数据库读取上次对话；`/new` 则绑定空会话。
验证命令：`uv run pytest tests/test_persistent_runtime.py`，重点见
`test_pending_history_is_distinct_from_replayable_and_full_history` 和
`test_restart_restores_previous_topic_and_new_conversation_is_empty`。
