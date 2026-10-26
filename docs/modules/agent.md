# Agent Loop

先读[整体架构与 Pipeline](../architecture.md)可了解本模块在启动、持久化和恢复路径中的位置。

## 职责与架构组成

`src/personal_agent/core/agent.py` 的 `Agent` 负责一个会话的有界模型/工具循环。它依赖 `Provider` Protocol、`ToolRegistry` 和 `ContextBuilder`，不依赖 CLI 或 SDK 响应对象。统一消息和结果来自 `core/models.py`，包括 `AssistantMessage`、`ToolMessage`、`ToolResult`、`RunResult`。 `asyncio.Lock` 串行处理同一实例的多个用户轮次，`max_rounds` 限制每次 run() 的正常模型请求次数；摘要调用另有独立次数、输入/输出和超时上限。

## 代码实际运行路径

1. CLI 的 `src/personal_agent/main.py::chat()` 调用 `Agent.run(user_input, permissions=...)`。
2. `run()` 拒绝空输入，获取会话锁，调用 `core/context.py::ContextBuilder.begin()`，先原子创建 Run 及原始用户消息，只构建 Base 和本次输入，再进入 `_run()`；历史读取留在可处理失败的准备阶段。
3. `_run()` 开始一轮模型请求。若还有后续请求预算，则通过 `tools.definitions(permissions=...)` 提供本轮授权的工具；最后一轮提供空列表，预留给最终回答。`max_rounds=1` 时第一轮就是最后一轮。
4. await `ContextBuilder.prepare(messages, definitions, provider)`，它编译最新 Core、检查本轮全部输入预算，必要时先执行无工具分批摘要并提交检查点。通过后才在 asyncio.timeout(request_timeout_seconds) 内 await Provider.generate(max_output_tokens=reserved_output_tokens)。返回的文本、调用、Provider JSON 和可选 TokenUsage 均是内部普通模型；usage 仅用于安全诊断。
5. 检查调用 ID 是否与历史或本次其他调用重复，检查是否既无文本也无工具调用。校验失败时不执行工具，返回 `invalid_response`。
6. 通过 `ContextBuilder.record()` 将有效响应先写入运行日志，再加入当前上下文。没有工具调用时，将文本作为最终回答，持久化完成状态后返回 `completed`。有工具调用时，即使同时有文本，也不将文本当作最终回答。
7. 非终轮按顺序 await `ToolRegistry.execute(call, permissions=...)`，逐个落库并加入带原调用 ID 的 `ToolMessage`，再开始下一次模型请求，让模型读取这些结果。
8. 终轮若仍收到工具调用，不调用 Registry；为每个调用生成 `ok=False`、 `error.code=round_budget_exhausted` 的结果，表示工具从未执行，然后返回 `max_rounds`。这既阻止无后续模型预算的新副作用，也避免历史留下未配对的调用。
9. 内部 `finish()` 在失败时追加停止说明，通过 `ContextBuilder.commit()` 保存本轮历史，完成 Run 状态转换，记录安全日志，并将 `RunResult` 返回调用方。

## 异常与边界

请求前记忆编译失败返回 `memory_error`，该次不调用 Provider；正常保存 failed Run。模型超时返回 `timeout`；Provider 失败返回 `provider_error`；非预期 Provider 异常返回 `runtime_error`。工具自身的校验、超时或执行错误以数据返回模型，不直接中止循环。终轮拒绝调用不影响此前的工具执行结果，不回滚此前副作用，也不自动重试。存储失败返回 `storage_error`，在调用记录落库失败时不执行工具，在结果落库失败时停止后续工具。用户取消会传播 `CancelledError`，将 Run 标记 cancelled；已写入的运行日志保留，已发生的副作用仍存在。 cancelled / interrupted Run 不进入后续模型上下文，但仍可从完整持久化记录读取。终轮策略不增加正常请求，无法在预算耗尽后保证生成自然语言回答。Compaction 不执行 Agent 工具、不消耗正常 round，但可能发起有独立上限的摘要请求。

## 阅读与验证

最小示例：`max_rounds=2`，第一轮请求 `add_numbers(2, 3)`，Registry 返回 `sum=5`，第二轮携带此结果、工具定义为空；模型回答“5”即完成。如果第二轮继续请求工具，则拒绝执行。

对应 `tests/test_agent.py`：

- `test_final_round_can_return_answer_after_processing_prior_tool_output`
- `test_final_round_never_executes_new_side_effects_even_with_text`
- `test_round_limit_rejects_final_tools_keeps_pairs_and_allows_next_turn`

运行：`uv run pytest tests/test_agent.py`。测试使用 Fake Provider，无真实 API 请求。持久化路径和中断测试见 `tests/test_persistent_runtime.py`。 V0.3 的本轮权限、跨会话记忆、每轮刷新和终轮写入拒绝见 `tests/test_memory_runtime.py`。权限默认空集合，由可信调用者显式传入，不从输入、历史或记忆推断。

## V0.6 Context 准备的失败路径

每轮正常 Provider 调用前均 prepare()，包括长工具返回后的下一轮；当前 Run 的调用/结果始终保留，不能在中间截断。ContextError 转 context_error 或 compaction_error，追加停止说明并正常保存 failed Run。准备阶段的 HistoryError 必须继续抛给 run() 的存储错误处理，不能被普通 Provider 的宽泛异常分支吞掉；摘要存储失败因而返回 storage_error 并中断 Run。取消仍由外层传播，检查点只保留已提交批次。

工作消息链会在 prepare() 中从有界历史和 pending 重建；ID 检查针对当前可见消息与新响应，历史调用不作为执行队列。Provider 原始数据在仍需重放的完整 Run 中保留。实际压缩路径、错误代码与配置见[Compaction](compaction.md)，验证见 `tests/test_compaction.py`，尤其是 current_run_growth、summary_write_failure 和 actual_process_exit 场景。
