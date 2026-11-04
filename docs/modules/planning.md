# Todo、Task 与 Goal

## 职责与架构组成

`planning/service.py::PlanningService` 管理当前来源的 Todo、Task 依赖与 Goal 验收，状态由 HarnessRepository 保存。Todo 描述执行步骤，Task 记录持久任务和回执；Episode/Thread 继续整理真实历史，提醒继续使用原 Scheduler。

## 机制流程

1. 用待办清单说明准备做什么，用持久任务记录依赖和执行状态，用目标定义最终如何验收。
2. 先检查依赖是否完成，再领取可做的任务；任务完成必须绑定真实成功回执，勾选清单不能代替证据。
3. 模型提出最终答案后检查目标要求：所有确定性检查通过才算通过；仍有后台工作则等待状态不能冒充完成。
4. 检查不足时最多有界继续；没有可用验收依据时明确未验证。取消目标只停止相关未完成工作，不撤销已发生动作。

## 代码实际运行路径

1. `HarnessRuntime` 注册 `todo_write`、Task 工具，并把 PlanningService 接到 Agent.goal_gate 和 ContextContributor。
2. `todo_write` 校验最多 20 项和至多一个 in_progress，覆盖当前来源清单的最新修订；重启只读取它，不自动执行。
3. `task_create` 分配 ID，创建 pending 记录。`task_update_dependencies` 只修改 pending 任务，检查同来源存在性与有界图的环；最多 100 个节点。
4. `task_claim` 检查全部依赖 completed，使用修订条件更新为 in_progress 并记录当前 Run。失败、取消、interrupted 依赖不解锁下游。
5. `task_complete` 要求实际成功工具回执。`receipt()` 查询原 messages/runs 并核对来源关联；模型陈述或 Todo 状态不能代替回执。fail/cancel 保存终态，启动 recover 只把遗留执行态改为 interrupted。
6. 明确 `/goal <条件> | <检查引用列表>` 创建 Goal，并立即把条件交给 Agent 执行。检查支持 `file:report.md`、`task:<id>`、`receipt:<call_id>`，裸 call_id 兼容回执检查；多个检查全部通过才成功。
7. Agent 得到无工具候选答案后调用 `evaluate()`。仍有后台作业时 waiting；确定性检查失败为 continue；无检查且未配置评审时 blocked，不将语义目标直接判成功。
8. `AGENT_GOAL_MODEL_REVIEW=true` 才启用独立无工具评审。评审使用共享预算、结构化布尔结果，不能覆盖确定性失败。反馈随持久 Goal 决策重新装配，最多续跑两次且不重置原轮数。

## 异常与边界

Goal 验证通过的是宿主建立的检查条件。`file:` 仅验证允许工作文件存在且可读，不证明正文语义质量；语义验收需启用评审或由用户审阅。Task 回执证明动作成功，不自动证明该动作满足任意自然语言目标。

`/goal cancel` 取消活动目标并取消关联后台作业，不能撤销已完成副作用。QQ 仅支持明确 `/goal`，Host 管理命令不远程开放。新会话不继承其他来源任务；无权限查看其他来源 ID。

## 阅读与验证

示例：`/goal 生成资料报告 | file:report.md`；工具流程为 create → dependencies → claim → 工作工具 → complete。`tests/test_harness.py` 覆盖依赖环、重复认领、伪造回执、重启状态与跨来源 Goal 隔离。

## 未来改进

为 Goal 增加宿主注册的内容检查器，使文件存在检查与语义验收明确分开。统一解释 Task 回执、Goal 决策和后台等待的关系，减少模型把计划勾选当作完成。
