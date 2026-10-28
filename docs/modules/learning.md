# P2 Memory Learning

P3 的 LEARN_EXTRACT 增加边界：Canon/示例/模拟角色心境不是用户事实，明确的简洁程度、菜单和行动清单偏好可以学习，但不能改写角色身份。目标检查与 Writer 未扩张，仍禁止 persona/index；角色包不在 MemoryFS 根目录，也不在 Learner 可写目标中。真实 P2 写入与角色上下文集成测试见 tests/test_character_relationship.py。

## 职责与组成

`learning/service.py::MemoryLearningService` 提供 Host 控制的自动学习，与手动 Reflection 共用 `ReflectionModel.ask()`、`reflection.evidence.memory_evidence/verify_candidates()`、`reflection.memory.plan_item()` 和 `reflection/writer.py`，没有新 Agent Loop。`learning/models.py` 限制来源、候选、目标文件、请求输入/输出和超时。`storage/learning.py::LearningRepository` 保存独立任务和来源处理记录，schema 11 追加对象，原迁移 1–10 不改。Markdown 是知识权威源；SQLite 只保存引用、哈希、状态及安全计量，不保存知识正文。

## 实际执行路径

1. Host 调用 `learn(run_ids=...)` 或 `learn(episode_ids=...)`。`snapshot()` 重新 JOIN Agent/Conversation/Run，拒绝非 completed Run；Episode 必须 finalized。先查询数量/字节预算，再通过原 `read_source()` 加载完整 Run。跨 Conversation 的来源分别携带真实归属、started_at/ended_at，并提供独立处理时间 as_of；按真实来源开始时间排序，相对时间不能按处理日期猜测。摘要只是定位提示。
2. `create()` 以来源 ID/revision 去重，`_execute()` 先保存 processing。`propose()` 直接 Extraction，允许空列表，不强制 Worthiness。`verify_candidates()` 对每个引用复用 core.evidence 的精确引文校验，confirmed 必须有真实用户消息，不能仅引用 Assistant 或 Tool。
3. Selection 返回每个目标及 candidate_ids，可另外选有界相关文件进行对照。Host 要求候选完整覆盖、路径唯一、有界；只读真实 catalogue 中的现有路径。拒绝 persona.md 和索引直接写入。
4. 每个目标一次 Consolidation，按实际来源时间排序所有候选，更新当前认识、替换明确过时状态，重复/不确定结果可 no-op/review。`plan_item()` 校验唯一 old_text、expected_hash、大小、Frontmatter 和主题冲突。所有目标计划完成后才开始写入。
5. 再次校验原始来源哈希及 Episode revision；写入前保存仅含路径、前后哈希的回执。`write_item()` 同步复用 MemoryFS/index.create/index.edit，短锁、Git 隐私、Agent 路径检查和原子文件发布全部保留。每个目标最多一次写入，目标之间让出事件循环。
6. 文件成功后记录回执，全部终结后任务 completed。空提取、全 no-op 同样完成来源；不修改 Reflection、Episode、Thread 的独立进度。

## 异常与边界

取消模型等待恢复 pending，不推进来源；已准备/发布文件的任务保守进入 requires_reconciliation。重启 recover() 先将无回执 processing 恢复 pending，有回执转为待核对。构造服务后 recover_pending() 在没有该 Agent running Run 时检查最多一个遗留任务；Runtime 仅在 Idle 再逐轮检查一个。reconcile(job_id) 只读比较前后 hash 和索引：after 且索引完整为 applied；before 或 create 仍不存在为 not_applied；第三种 hash、不可读或索引不完整继续保护，不直接重发旧 CREATE/UPDATE。全部 applied 才 completed；混合已发布/未发布则保留已发布回执、清除旧待写计划，从当前 Markdown 重新提取/选择/整合。该任务中已确认发布路径不能被重新规划再次写入，模型提出这类更新会明确失败而不是重复追加。未发布部分不能只因 A 成功就视为完成。

恢复元数据复用 Job metrics.recovery（无正文），不新增 schema：默认同一不确定任务最多 3 次检查、间隔 300 秒，一轮最多 9 个回执、2 秒协作式检查预算。同步磁盘 IO 不具备硬取消保证；预算在文件间检查。安全重新规划最多 3 次，此后仅把已证明无不确定副作用的任务标为 failed，等待显式 retry，不继续制造全局不明确状态。原失败仍需显式重试，真正冲突/不完整索引达到检查上限后保留 requires_reconciliation；索引修复沿用显式 /memory repair，再核对/重试。Status attention 暴露错误、次数和下次检查时间。前台 Run 或 Learner 锁被占用时不检查。

剩余未核实任务仍是 Agent 级写保护；Selection/Consolidation/Curator 有跨文件比较依赖，本轮没有引入未经证明的文件级并行。ready() 和实际 _execute() 都检查保护，可信调试入口也不能绕过。取消后的常见可证明状态自动恢复，人工修改不会被覆盖；不能验证的情况仍需用户处理。文件成功而 SQLite 失败不宣称撤销，原 Shared Writer、路径、隐私、expected_hash 与单文件原子发布不变，Main Agent 没有获得新权限。

Host 校验结构、真实引文与写入边界，不能数学证明模型的语义判断。假设、外部指令、未接受建议、敏感推测和冲突应由 Extraction/Consolidation 拒绝或标为不确定；原始来源沿用既有脱敏，响应及待发布片段做保守凭据模式检查，不宣称完备秘密识别。FakeProvider 测试不代表真实语义质量。

## 双速自动化的实际路径

`runtime.assemble_chat()` 装配一个 Learner 并传给 `OrganizationRuntime`。首次 enabled 时 `activate()` 保存 Run/Episode rowid 水位；旧数据不静默重复学习，水位跨重启保留。`poll()/harvest_priority()` 每次最多扫描 16 个新 Run 的首条用户文本，用保守的记住/偏好/正式决定/明确更正等信号入队，不发分类 LLM。processing/failed/空结果与 completed 分开记录。

`learn_priority()` 尽快在回复之后的后台执行；`learn_batch()` 只在 Idle 处理 finalized Episodes，默认 3 个或最早来源等待 21600 秒。它们最终复用同一 `_execute()`，不复用 Reflection 进度。Priority 完成不会跳过包含其他 Run 的整个 Episode；Batch 再次读取现有 Markdown，由 Consolidation 判定重复 no-op。一次任务失败需要显式 retry，取消在没有文件回执时恢复 pending。

Runtime 统一包装 Episode/Thread/Learning 的 Provider，维护轮共享 4 批、4 请求、131072 保守预留 tokens、120 秒；批间让出、60 秒冷却。Dirty Repair 优先，学习与其他维护交替获得优先机会，每轮最多一个学习流水线。开始付费提取前保守检查整个流水线能否放入剩余请求/token 预算，避免预算不足反复支付 Extraction。自定义预算无法容纳时 status 的 learning_last_report 显示 learning_budget_config，保留待处理工作而不调用模型。

P2 enabled 时旧 MemoryAutomation 不再自动消费新 Run，旧计划、审批与数据全部保留；`/reflect` 仍可手动调试。关闭 AGENT_MEMORY_LEARNING_ENABLED 后才恢复原 Automation 模式；不同时自动执行两者。`/memory learning status|run|retry|reconcile <job_id>` 是可信调试入口，run/retry 使用共享预算和整轮超时；不向 Main Agent 注册自动写工具。原 /memory repair 可先补齐部分失败索引，再进行只读 reconcile。

存在 requires_reconciliation 时，自动学习暂停，防止新的文件更新破坏已保存回执的核对依据。所有配置在 `.env.example`；自动任务不主动打印正文。`tests/test_learning_runtime.py` 覆盖首启水位、Priority→Batch 不漏其他 Run、单 Episode 六小时机会、抢占、旧 Automation 不执行、空提取推进、失败显式重试及共享预算。FakeClock 不等待真实数小时。

## 阅读与验证

最小入口：`await learner.learn(run_ids=[completed_run_id])`；`tests/test_learning.py` 使用隔离 MemoryFS/SQLite，覆盖偏好更新、同文件合并、跨 Conversation、空提取/no-op、禁止路径/Persona、来源伪造、取消与失败显式重试，以及文件成功/SQLite 失败后只读核对。原 `tests/test_reflection.py`、`tests/test_automation.py` 验证审批和旧记录保持兼容。

## Observatory 只读构造

`MemoryLearningService(..., initialize=False)` 仅由 observer-only 装配使用，跳过构造时的 activate/recover/recover_pending；学习策略和正常聊天构造行为不变，HTTP 不调用 learn/poll。参见 `tests/test_dashboard_lifecycle.py:test_observer_assembly_does_not_recover_or_publish_memory`。

## 发布作用域

MemoryLearningService.publication() 持有同一 asyncio 锁，向 Curator 提供仅在作用域内使用的发布回调；实际回执/取消/恢复仍由原 _execute() 管理。Learner 的 model 现在是独立 ReflectionModel，只负责无工具结构化请求，不携带手动计划 Repository 或审批入口。恢复核对使用真实 PlanItem，不再用 SimpleNamespace 模拟 writer 参数，持久回执字典及 schema 不变。
