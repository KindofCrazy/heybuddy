# General Agent Harness（P5）

## 职责与架构组成

`harness.py::HarnessRuntime` 在共享应用中装配工作文件、规划、Skills、MCP、子执行和 Workflow，CLI、官方 QQ 与 NapCat 使用同一实例。`core/agent.py::Agent` 保留手写有界循环；`tools/registry.py::ToolRegistry` 始终承担授权和参数校验。`core/lifecycle.py::HookRegistry` 提供不可变元数据事件，观察者不能替换模型请求或工具结果。

`core/execution.py::RunScope` 使用 ContextVar 保存 Host 绑定的 Agent、Conversation、来源、根执行 ID、权限和共享预算。工具参数不能填写这些身份。`ExecutionBudget` 默认最多 24 次模型请求、131072 预留 Token、180 秒和 256 次工具尝试，生产工具上限同时受原配置约束。`BudgetProvider` 在请求前保守预留，成功或失败后记录实际 usage；usage 缺失不返还预留量。主模型、摘要、Goal 评审与子 Agent 共享根预算，既有维护预算独立。

`storage/harness.py::HarnessRepository` 是 schema 23 的持久化入口：`harness_records` 保存按 Agent/Conversation/source 隔离的修订状态，`harness_events` 保存状态转换关联，`tool_artifacts` 保存有界工具正文。状态修改与事件原子提交，更新采用 revision 条件检查。原始会话消息继续由原 Journal 保存。

## 机制流程

1. 把一次任务绑定到明确的用户身份、会话、消息来源和根执行编号，后续状态按这些边界隔离。
2. 给任务一份共享额度，主模型、压缩、评审和子执行都从中消耗，不能通过拆任务重复领取。
3. 将允许的工具、操作指南、任务计划和后台结果接入同一执行过程，同时保留各自权限检查。
4. 执行结束后记录预算和结果；重启只展示中断状态，只有具备核对规则的固定流程才能显式恢复。

## 代码实际运行路径

1. `runtime.assemble_chat()` 构建原 Agent 与维护服务，再按 `Settings.harness_enabled` 装配 HarnessRuntime。此时只做本地配置与状态核对，不启动外部 MCP 连接。
2. `runtime.open_chat()` 调用 `HarnessRuntime.start()` 连接 Host 配置的 MCP Servers。单 Server 失败不阻断其他 Server 或普通对话；observer-only 不启动连接、不核对状态、不注册写工具。
3. CLI 调用 `ChatService.run()`；QQAdapter 在既有身份验证后传入 `scheduler.chat.source_scope(inbox.scope)`。NapCat 复用同一 Adapter，不从消息正文取得来源。
4. `HarnessRuntime.run()` 建立本次 RunScope，更新当前 Context 的 contributor 与 ReplayView 工厂。`Agent.run()` 在会话锁内创建 Journal Run，绑定 CURRENT_RUN，再通过 `on_begin` 保存来源关联。
5. Agent 发出 RunStarted；每轮准备预算化上下文，发出 BeforeModelCall，执行模型请求，记录已消费工具结果，发出 AfterModelCall。
6. 模型工具调用先落库。Registry 检查工具、逐 Run 权限、参数、共享工具预算和 ToolPolicy，再发出 PreToolUse。拒绝或 Hook 故障作为原 call_id 的失败回执返回。
7. 工具结果先落库，再触发 PostToolUse。普通观察 Hook 的故障只记录事件名和异常类型；Stop 在终结路径通知。取消继续向调用者传播，不伪造工具回执。
8. 无工具回复先经 GoalGate；通过后原子结束 Run，未通过则按有界续跑或未验证状态结束。结束后保存根预算报告，后台结果只确认本轮实际装配过的通知。
9. 应用退出关闭后台作业、MCP 和原维护服务。重启将遗留 Task、Execution、Workflow 执行态标为 interrupted，不启动旧调用。

Hook 默认单次 1 秒、单事件累计 2 秒；只有 PreToolUse 可阻断。强制 Journal 与 `RunTrace` 不依赖可选 Hook。

## 异常与边界

- 文件副作用和 SQLite 无共同事务。工具成功而保存失败时保留 storage_error/不明确状态，不回滚文件、不自动重做。
- ContextContributor 每项最多 32 KiB，仍计入 ContextBuilder 的总预算。超限明确停止，不静默删掉任务状态。
- 新增 RunResult 的 goal_unverified、budget_exhausted 仍映射到数据库 failed。任务状态不混入 Run 生命周期。
- 关闭 `AGENT_HARNESS_ENABLED` 停止注册新能力，不删除状态、不降级数据库。旧二进制不保证兼容 schema 23。源码发行包使用显式目录清单，排除本地运行目录、缓存及历史工作副本。
- `dashboard.app` 的 `/api/harness` 只读返回当前 Agent 最近 50 项状态元数据，不产生模型请求、不确认通知、不修改记录。

## 阅读与验证

可信 CLI：`/harness` 查看当前来源状态；`/goal` 查看目标；`/compact` 显式压缩；`/workflow resume <id>` 显式恢复。

运行 `uv run pytest tests/test_harness.py` 验证 Hook 顺序、失败、来源隔离、共享预算及端到端装配。运行 `uv run python -m personal_agent.evaluation.harness` 执行固定合成场景；这是离线机制验证，不衡量真实模型能力。

## 未来改进

将来源绑定、预算和扩展依赖声明为明确类型，减少 ContextVar 与装配细节的隐含耦合。为主请求、子执行和后台分别呈现累计额度，但保持共享根预算。
