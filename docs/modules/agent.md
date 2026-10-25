# Agent Loop

## 职责与架构组成

`src/personal_agent/core/agent.py` 的 `Agent` 负责一个会话的有界模型/工具循环。
它依赖 `Provider` Protocol、`ToolRegistry` 和 `ContextBuilder`，不依赖 CLI 或 SDK 响应对象。
统一消息和结果来自 `core/models.py`，包括 `AssistantMessage`、`ToolMessage`、`ToolResult`、`RunResult`。
`asyncio.Lock` 串行处理同一实例的多个用户轮次，`max_rounds` 限制每次 `run()` 的模型请求次数。

## 代码实际运行路径

1. CLI 的 `src/personal_agent/main.py::chat()` 调用 `Agent.run(user_input)`。
2. `run()` 拒绝空输入，获取会话锁，调用 `core/context.py::ContextBuilder.begin()`，
   由 `build()` 加载可重放历史、构建 system 和新用户消息，原子创建 Run 及用户消息，再进入 `_run()`。
3. `_run()` 开始一轮模型请求。若还有后续请求预算，则通过 `tools.definitions()` 提供工具；
   最后一轮提供空列表，预留给最终回答。`max_rounds=1` 时第一轮就是最后一轮。
4. 在 `asyncio.timeout(request_timeout_seconds)` 内 await `Provider.generate()`。
   Provider 的文本、工具调用和不透明 JSON 数据被转换为内部 `ModelResponse`。
5. 检查调用 ID 是否与历史或本次其他调用重复，检查是否既无文本也无工具调用。
   校验失败时不执行工具，返回 `invalid_response`。
6. 通过 `ContextBuilder.record()` 将有效响应先写入运行日志，再加入当前上下文。
   没有工具调用时，将文本作为最终回答，持久化完成状态后返回 `completed`。
   有工具调用时，即使同时有文本，也不将文本当作最终回答。
7. 非终轮按顺序 await `ToolRegistry.execute()`，逐个落库并加入带原调用 ID 的 `ToolMessage`，
   再开始下一次模型请求，让模型读取这些结果。
8. 终轮若仍收到工具调用，不调用 Registry；为每个调用生成 `ok=False`、
   `error.code=round_budget_exhausted` 的结果，表示工具从未执行，然后返回 `max_rounds`。
   这既阻止无后续模型预算的新副作用，也避免历史留下未配对的调用。
9. 内部 `finish()` 在失败时追加停止说明，通过 `ContextBuilder.commit()` 保存本轮历史，
   完成 Run 状态转换，记录安全日志，并将 `RunResult` 返回调用方。

## 异常与边界

模型超时返回 `timeout`；Provider 失败返回 `provider_error`；非预期 Provider 异常返回 `runtime_error`。
工具自身的校验、超时或执行错误以数据返回模型，不直接中止循环。
终轮拒绝调用不影响此前的工具执行结果，不回滚此前副作用，也不自动重试。
存储失败返回 `storage_error`，在调用记录落库失败时不执行工具，在结果落库失败时停止后续工具。
用户取消会传播 `CancelledError`，将 Run 标记 cancelled；已写入的运行日志保留，已发生的副作用仍存在。
cancelled / interrupted Run 不进入后续模型上下文，但仍可从完整持久化记录读取。
此策略不增加额外请求，无法在预算耗尽后保证生成自然语言总结。

## 阅读与验证

最小示例：`max_rounds=2`，第一轮请求 `add_numbers(2, 3)`，Registry 返回 `sum=5`，
第二轮携带此结果、工具定义为空；模型回答“5”即完成。如果第二轮继续请求工具，则拒绝执行。

对应 `tests/test_agent.py`：

- `test_final_round_can_return_answer_after_processing_prior_tool_output`
- `test_final_round_never_executes_new_side_effects_even_with_text`
- `test_round_limit_rejects_final_tools_keeps_pairs_and_allows_next_turn`

运行：`uv run pytest tests/test_agent.py`。测试使用 Fake Provider，无真实 API 请求。
持久化路径和中断测试见 `tests/test_persistent_runtime.py`。
