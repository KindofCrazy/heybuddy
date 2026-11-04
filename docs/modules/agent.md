# Agent Loop：响应、工具批与停止

## 职责与架构组成

`src/personal_agent/core/agent.py::Agent` 管一次输入的锁、模型循环、响应检查、工具顺序与 RunResult。ContextBuilder 管上下文和持久化协议，Provider 管模型协议，ToolRegistry 管工具执行。Core 不依赖 SDK 响应对象或 SQLite。

## 代码实际运行路径

1. run 验证输入、取得实例锁，重置 round/request 计数，设置 ContextVar，用 BudgetProvider 包装 Provider。
2. context.begin 原子创建 Run/用户消息，on_begin 绑定根执行，发 RunStarted。
3. _run 每 round 筛选工具，最后一轮传空列表；prepare 组装请求。
4. BeforeModelCall → _generate 保存请求审计并等待模型。明确超限可在同 round 重新 prepare、补救一次。
5. 成功后 mark_consumed → AfterModelCall → observe_usage。检查调用数量、重复 ID、空响应，合法才 record 完整 AssistantMessage。
6. 有工具则逐条 execute → record ToolMessage → PostToolUse，批后进入下一 round。工具失败通常作为数据交给下一轮。
7. 无工具时由 GoalGate 验收，最多两次 continue 且不重置轮数。通过则 completed，否则 goal_unverified。
8. finish 对非 completed 追加停止审计，然后 commit。finally 发 Stop，恢复 Provider/ContextVar。

最后一轮违规返回工具时，保存调用和 round_budget_exhausted 拒绝结果，不执行，最终 max_rounds；正常文本仍可 completed。完整停止表见[消息主线](../architecture.md)。

## 异常与边界

默认 max_rounds=10、单响应工具上限 64、单 Run 256，生产值由宿主配置。HistoryError 停止并返回 storage_error；取消 abort 后继续传播。已发生副作用不回滚。实例锁只串行化该执行器。

round 不等于 Provider 请求数：摘要、一次超限补救与可选评审可能增加请求。_request_number 只数普通 _generate 调用，根预算覆盖共享 Provider 请求。

## 阅读与验证

`uv run pytest tests/test_agent.py tests/test_model_rounds.py tests/test_persistent_runtime.py`。直答一轮，“查时间→工具结果→文本”两轮；同响应两个工具仍只占一轮，按顺序执行。

## 未来改进

- 将响应验证、停止决策提成纯函数与明确枚举，减少循环内分支。
- 区分候选答案、验收答案、已提交答案，为未来流式展示建立清晰边界。
