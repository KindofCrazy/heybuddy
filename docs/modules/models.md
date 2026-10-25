# 内部数据模型

## 架构组成

`core/models.py` 定义 Runtime 消息、ToolCall、ToolResult、ModelResponse 与 RunResult。
Message 是以 role 区分的 Pydantic 联合类型；Provider 数据只能保存普通 JSON。
`storage/models.py` 定义 AgentRecord、ConversationRecord、StoredMessage、RunRecord，
将持久记录身份与运行时 `core/agent.py::Agent` 的执行职责分开。

## 实际数据路径

1. 用户输入转换为 TextMessage；Provider 响应归一化为 ModelResponse 和 AssistantMessage。
2. ToolCall 保留调用 ID、名称和原始参数 JSON 字符串；ToolResult 保留相同 ID、成功状态、数据或错误。
3. SQLiteStore 用 `TypeAdapter(Message).dump_json()` 保存完整结构，而非拼接展示文本。
4. 重载时 validate_json() 恢复带 role 的实际消息类型；provider_data 按原 JSON 返回 Provider。
5. StoredMessage 在内容之外增加独立 ID、conversation_id、run_id、稳定 sequence 和创建时间。
6. RunResult 表示本次 Runtime 的完成或停止原因；RunRecord.status 表示持久运行生命周期，
   比如 provider_error 的 RunResult 对应已正常 finalized 的 failed RunRecord。

## 异常、示例与验证

模型禁止额外顶层字段，未知 role 或不符合模型的存储内容会拒绝重放，避免静默丢字段。
provider_data 的命名空间内容不由 Loop 解释，保留 reasoning 和函数调用项目的原始顺序。
`storage_error` 表示存储故障，不能视为无工具副作用；cancelled / interrupted 没有伪造的正常最终结果。
示例：一条 AssistantMessage 含两个 ToolCall，后续两条 ToolMessage 分别关联它们的 ID。
`uv run pytest tests/test_storage.py tests/test_persistent_runtime.py` 验证完整 JSON、Unicode、
嵌套 Provider 数据及工具结果在多次重启后不丢失。
