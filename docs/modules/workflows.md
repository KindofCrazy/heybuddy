# 注册式 Workflow 与离线评测

## 职责与架构组成

`workflows/runtime.py::WorkflowRegistry` 注册固定版本 WorkflowDefinition，WorkflowStep 包含固定宿主操作、依赖、超时、当前权限要求和恢复类别。模型只能提供通过 input_schema 校验的数据，不能提交可执行 Workflow 代码。

## 代码实际运行路径

1. 注册时校验输入/输出 Schema、步骤名与 DAG，最多 100 步，拒绝外部 Schema 引用。
2. workflow_start 校验输入，保存版本、输入 hash、running 状态及空步骤集合。每个步骤执行前保存 attempt；按依赖以最多两个并行步骤推进。
3. execute 检查当前 RunScope 权限，保存 running，再在步骤超时内等待 operation。JSON 可序列化的结果与 completed 检查点一起保存；最终结果通过 output_schema 才标 Workflow completed。
4. 异常保存 blocked；取消等待 TaskGroup 清理后保存 interrupted。执行中不做工具回滚、不自动重试步骤。
5. `/workflow resume <id>` 仅可信 CLI/Host 开放。核对归属、版本、输入 hash 与当前权限，复用已完成结果。
6. 未完成只读步骤可以显式重做；idempotent/unsafe 步骤必须先调用注册的只读 reconcile。确认为 completed 则采用回执，确认为 not_started 且可安全重做才执行；其余 blocked。
7. Workflow completed 只代表固定流程成功，Agent 的 GoalGate 仍独立判断用户要求。

首个 working_report Workflow 从工作区读取指定来源，以确定性摘录组织主题，创建报告并核对输出 hash 与来源。写入步骤提供内容 hash 核对，不覆盖已经变化的报告。这是可验证的首个固定流程，不声称模型完成了语义研究。

## 异常与边界

状态与外部副作用不能共同事务提交。已发生但未确认的操作必须核对；没有 reconcile 的非只读步骤保持 blocked。未配置的 Workflow 不可启动，不能在 QQ 远程 Resume，不在重启时自动执行。

`evaluation/harness.py` 只使用固定 ScriptedWorker、隔离 SQLite/工作区和本地 Skill/MCP Schema fixture，对比工具、Task/Goal、Skill/MCP、子执行/Workflow 场景。Token usage 为合成计数；真实 MCP transport 独立由测试覆盖。指标不代表真实模型能力或算法收益。

## 阅读与验证

模型工具调用：`workflow_start(name="working_report", arguments={"sources":["source.md"],"output":"report.md"})`。离线机制探针：`uv run python -m personal_agent.evaluation.harness`。

`tests/test_harness.py` 覆盖报告产物、只读步骤显式恢复、已完成步骤不重跑、未知副作用不重复，以及输入/版本/当前权限边界。
