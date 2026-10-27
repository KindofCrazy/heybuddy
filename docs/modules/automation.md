# Memory Automation：回答完成后的有界学习

## 职责与架构组成

`automation/service.py::MemoryAutomation` 是轻量应用服务，在普通 Run 完成并向用户展示答案后顺序执行。它不创建新 Agent Loop，不在每个工具 round 后运行，不启动后台任务。`automation/models.py` 定义可信 AutomationPolicy、严格 Worthiness 模型；`automation/meter.py::MeteredProvider` 只计量自动记忆额外请求；`storage/automation.py::AutomationRepository` 绑定 Agent 保存状态。ReflectionService、MemoryFS、ToolRegistry 和原 Journal 继续承担原职责。

LLM 决定历史有无长期价值、如何提取/去重/纠正与组织知识；宿主决定触发时机、源范围、预算、权限、来源验证和实际发布。Worthiness 不授予权限，模型输出多一个 approved 字段会被严格 Schema 拒绝。

## 代码实际运行路径

1. `_run_session()` 用同一 Provider/MemoryFS 构造 ReflectionService，再用 Settings.automation_policy() 构造 MemoryAutomation。取得数据库占用锁后的启动阶段调用 repository.recover()，仅把未结束自动检查标记 interrupted，不执行模型/工具。
2. `chat()` await Agent.run()；只有 completed 结果先打印 Agent 答案，再展示 Memory 后续检查提示，await automation.run(current_conversation_id)。off 不检查；普通管理命令不创建 Run，也不触发该钩子。
3. `_run()` 先查活动 Reflection Plan。有未审阅计划时返回 pending_review 和 unprocessed_runs，不覆盖计划、不重复调用 Worthiness，后来的原始聊天照常保存。
4. 没有计划时，活动 failed/interrupted batch 默认阻止重做；用户显式 `/memory automation retry` 才允许重试未完成阶段。若进程曾在保存计划后、关联 batch 前退出，plan_for_batch() 按范围找到并验证原计划，补记关联而不重新提取或写文件。
5. 通过 ReflectionRepository.window() 从独立审阅边界读取有界 completed Run。逐完整 Run 验证 Worthiness 输入及后续 Extraction 均可容纳，随后 begin() 记录 running batch 及来源 Run/hash。没有历史直接返回；单 Run 太大明确诊断，不能先全量读取再截断。
6. `_request()` 将原始证据投影和 Core 作为低信任数据，专用 WORTHINESS_PROMPT 要求 should_reflect、reason、category、confidence JSON；空值判断是正常结果。无工具、4096 总输出 tokens、2048 字节正文上限，输入受配置/模型余量限制。
7. 成功判断无价值：在同一短事务验证源范围/当前进度、保存 not_worthy 和 decision、推进 reflection_progress。该边界表示历史已被成功筛查，不表示有提案或写入；batch.decision=false、plan_id=NULL 明确保留区别。失败不推进，原始消息不删除。
8. 值得反思：先保存 worthy/decision 和已知用量，再创建共享相同 Repository/Memory/Policy 的 ReflectionService，唯一差别是 Provider 套上本批 MeteredProvider。reflect(through=batch.through_sequence) 只处理本次判断的前缀；复用原 Extraction/Selection/Consolidation/证据与片段校验，不复制这些逻辑。
9. 保存原 Reflection Plan，关联 plan_created 和候选数；默认 suggest 到此结束，提示 /reflect review。待审条目、拒绝、skip、apply、reconcile 与人工计划使用同一接口/恢复规则。
10. auto 模式仅检查可信精确授权，符合才调用原 apply(..., approval_source="host_exact_grant")；所有路径/hash/容量/隐私/索引校验继续执行。成功、冲突、partial/applying 仍以原计划为准，不重新执行历史工具或盲目重写。

## 模式与可信授权

- off：关闭自动检查，手动 /reflect 保留。
- suggest：默认。自动生成计划，用户 approve/apply 后才写 Markdown。
- auto：仅支持宿主事前提供的精确 create 授权，不是通用自主写入。AGENT_MEMORY_AUTO_CREATE_GRANTS 是 JSON 对象 `{ "knowledge/topic.md": "<完整 Markdown 的 SHA256>" }`。只有 confirmed 的 knowledge/project、knowledge/ 下普通主题新文件、最多 4096 UTF-8 字节且 proposed after_hash 完全相符才可自动应用。Core、更新/删除、correction、不确定候选、未授权内容、路径或摘要不符都继续人工审阅。

选择精确内容授权，是因为宿主无法仅凭模型声称“非敏感”来证明任意生成内容安全。授权的是已审核的具体 Markdown，不是主题名、目录白名单或 approved=true。没有 grants 时 auto 也不会自动写。它适用于已批准内容的受限发布；不是任意新知识自动写入。授权须在该计划生成前配置，已有 pending 计划仍走 /reflect apply。用户可在 review 查看 Proposed hash，但为已有计划重复配置授权没有必要。

approval_source 与 approved_at 随原计划保存，区分人工与精确 Host Grant。模型不接收、更不能修改 grants、mode 或配置。配置错误在 Settings 阶段拒绝；正常聊天里的“启用 auto”不改变模式。

## SQLite schema 5 与恢复

migration 5 只追加 automation_batches、每会话一个未完成 batch 的唯一索引及范围查询索引。保存 id/conversation、首尾边界、来源 hash、state、decision、plan_id、metrics 和安全 error_code。没有新 Memory 权威副本或队列；Worthiness、进入 Reflection、计划、批准和实际应用通过 batch 和原计划的不同字段/状态表示，不能用单个 processed=True 替代。

成功的 not_worthy 在同一事务推进审阅进度；worthy 不能推进未审阅计划的边界。生成计划后仍未批准时进度保持原值。用户处理所有条目后原 Reflection 事务推进边界；下一次 completed 输入或显式 run 继续剩余历史。Compaction 边界完全独立。

Provider/JSON/来源/预算/数据库失败保留诊断，已结束普通 Run 不改为失败。取消后普通答案已提交，自动检查记 interrupted；CLI 可以退出，重启不自行重试。成功 Worthiness 已持久化时，显式 retry 复用它，只重试 Reflection；失败 Worthiness 不当作有效判断。源内容变化或进度冲突会阻止旧范围继续使用。

文件与 SQLite 没有共同事务；plan_created 先关联原计划，再逐项发布，已发布文件的 applying/partial 恢复仍使用 /reflect reconcile，不自动回滚或重写。批次与计划关联之间的崩溃仅补记现有计划；判断尚未提交时再次付费调用需要明确 retry。

## 成本、等待与诊断

默认每次最多 4 次额外模型请求（Worthiness 一次 + Reflection 至多三次），保守输入加预留输出总量 131072，整段等待最多 180 秒；底层各阶段仍有独立超时。没有后台并发，答案先显示，但输入提示要等检查返回；Ctrl+C 可退出。同步短 SQLite/Memory 步骤不是可硬取消 I/O。

MeteredProvider 分开记录自动请求数、预留 token、已报告 input/output/reasoning、usage_complete、耗时及候选数；reasoning 包含在 output 里，不能再次相加。Usage 缺失时计数仅表示已知部分，不是精确零费用；进程崩溃期间可能有未知调用，诊断不是账单。明确 retry 累计已知计数，预算约束每次用户触发的尝试。不从价格表自动猜账单。

status 返回 batches/decisions/triggers、原审阅边界、pending plan、未处理 completed Run 数和最新安全计量/错误；不打印历史正文、Memory、候选或 Worthiness.reason。review 才显示私人提案。

## 阅读与验证

CLI：`/memory automation off|suggest|auto|status|run|retry`；mode 切换仅当前进程生效，重启从可信配置恢复。run 显式检查积压历史；retry 显式恢复失败阶段。已有人工 /reflect 全套命令不变。

最小离线示例见 tests/test_automation.py：seed completed Run → Fake Worthiness → 原 Reflection 三阶段 → suggest 未改文件 → /reflect apply → 新会话读取最新 Core。测试还覆盖负判断/临时问题、待审积压、拒绝、调用上限、数据库回滚、取消、Scope、精确 auto 授权和真实子进程在 worthy 后退出/恢复。原测试网络保护不变。

## V0.7.2 Phase 2：Worthiness 选择与实际限制

WORTHINESS_PROMPT 增加“新增信息”的判定：重复已提供的 Core/已读取记忆不触发；如果尚无法确认既有正文，交 Reflection 比较。重复场景的两次运行自动请求 5 → 2，不能据此声称通用精准率提高。最终 Holdout 总分持平，自动阶段耗时反而增加，详见 [评测报告](../prompt-ab-results.md)。

原始来源预算包含 Provider JSON；单 Run 超 16KiB 时可在进入 Worthiness 前失败，保留普通答案与未处理历史，不截断成假成功。本次基线一次长假设回答触及此边界；这是已知可用性限制，没有为改善分数放松输入/输出预算。
