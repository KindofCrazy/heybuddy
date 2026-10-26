# 内部数据模型

## 架构组成

`core/models.py` 定义 Runtime 消息、ToolCall、ToolResult、ModelResponse 与 RunResult。 Message 是以 role 区分的 Pydantic 联合类型；Provider 数据只能保存普通 JSON。 `storage/models.py` 定义 AgentRecord、ConversationRecord、StoredMessage、RunRecord，将持久记录身份与运行时 `core/agent.py::Agent` 的执行职责分开。

## 不同 ID 分别关联什么

| 标识 | 生成/来源 | 关联职责 |
| --- | --- | --- |
| AgentRecord.id | 默认 heybuddy，或存储 API 创建的 ID | 找到共享身份及对应 Core Memory 命名空间 |
| ConversationRecord.id | 本地 UUID | 选择、切换和隔离会话 |
| RunRecord.id | 本地 UUID | 将一次用户输入的所有持久消息分为一组 |
| StoredMessage.id | 本地 UUID | 标识一条数据库记录，不承担排序职责 |
| StoredMessage.sequence | 会话内递增整数 | 决定消息重放顺序 |
| ToolCall.id / ToolResult.call_id | Provider 响应中的调用标识 | 把模型调用与工具结果配对 |
| Provider output item 的 id | SDK 响应项目 | 保留在 provider_data 中供原协议重放 |

例如 OpenAI function_call 的项目 id 与 call_id 可以不同；适配器取 call_id 作为内部 ToolCall.id，工具结果必须关联这个值。不能用数据库 Message ID 或 SDK 项目 id 替换工具调用 ID。

AgentRecord.system_prompt 保留为兼容元数据，CLI 不从中加载 Base。V0.5 的 `memory/models.py::CoreMemoryData` 只持三个块及当前文件摘要，替代 MemoryRevision/MemorySnapshot；摘要派生自普通 Markdown，不保存历史，也不新增 SQLite Memory 表。`render_memory()` 只注入 kind/trust/blocks，读取工具返回 hash 供 expected_hash 检查。RunResult.memory_error 表示请求前编译失败，正常保存停止记录后持久 Run 状态为 failed。

## 实际数据路径

1. 用户输入转换为 TextMessage；Provider 响应归一化为 ModelResponse 和 AssistantMessage。
2. ToolCall 保留调用 ID、名称和原始参数 JSON 字符串；ToolResult 保留相同 ID、成功状态、数据或错误。
3. SQLiteStore 用 `TypeAdapter(Message).dump_json()` 保存完整结构，而非拼接展示文本。
4. 重载时 validate_json() 恢复带 role 的实际消息类型；provider_data 按原 JSON 返回 Provider。
5. StoredMessage 在内容之外增加独立 ID、conversation_id、run_id、稳定 sequence 和创建时间。
6. RunResult 表示本次 Runtime 的完成或停止原因；RunRecord.status 表示持久运行生命周期，比如 provider_error 的 RunResult 对应已正常 finalized 的 failed RunRecord。

## 异常、示例与验证

模型禁止额外顶层字段，未知 role 或不符合模型的存储内容会拒绝重放，避免静默丢字段。 provider_data 的命名空间内容不由 Loop 解释，保留 reasoning 和函数调用项目的原始顺序。 `storage_error` 表示存储故障，不能视为无工具副作用；cancelled / interrupted 没有伪造的正常最终结果。示例：一条 AssistantMessage 含两个 ToolCall，后续两条 ToolMessage 分别关联它们的 ID。 `uv run pytest tests/test_storage.py tests/test_persistent_runtime.py` 验证完整 JSON、Unicode、嵌套 Provider 数据及工具结果在多次重启后不丢失。


## V0.4 Recall 证据模型

`recall/models.py` 独立定义 SearchArguments、GetArguments、SearchHit、RecalledMessage、SearchResult 和 GetResult。证据包含稳定的原 Message ID、Conversation ID、Run ID、sequence、created_at、role、run_status，而非一条新聊天记录；FTS 内部 rowid 不暴露为长期外部定位标识。结果标记 historical_data_only、范围限制和文本截断。它们不包含完整 provider_data、原始工具调用或结果，完整审计仍使用原 StoredMessage 结构读取。

V0.5.1 的 `memory/models.py::summary()` 仅在提取目录概要时统一 LF/CRLF/CR，避免将换行差异误诊为父入口陈旧；不重写原文件字节。索引条目保留原始字符区间，由 index.edit()/maintain_link() 维护；执行与边界见[MemoryFS](memory.md)，验证见 `tests/test_deferred_memory.py::test_metadata_edit_updates_only_intact_generated_entry`。

V0.6 新增普通内部 TokenUsage(input_tokens, output_tokens)，ModelResponse.usage 可为空；用于区分服务端计量与本地估算，不把 SDK 对象放入 Runtime。RunResult 新增 context_error/compaction_error。`core/history.py` 的 ConversationSummary 保存 text/through_sequence/through_run_id/revision/updated_at/checksum；HistoryTurn 保存完整 Run 消息与来源 hash，HistoryPage 表示有界页及 has_more。Summary 与 StoredMessage 分离，执行路径见[Compaction](compaction.md)，测试见 `tests/test_compaction.py`。
