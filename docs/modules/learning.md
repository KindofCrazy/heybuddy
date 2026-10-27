# P2 Memory Learning

P3 的 LEARN_EXTRACT 增加边界：Canon/示例/模拟角色心境不是用户事实，明确的简洁程度、菜单和行动清单偏好可以学习，但不能改写角色身份。目标检查与 Writer 未扩张，仍禁止 persona/index；角色包不在 MemoryFS 根目录，也不在 Learner 可写目标中。真实 P2 写入与角色上下文集成测试见 tests/test_character_relationship.py。

## 职责与组成

`learning/service.py::MemoryLearningService` 提供 Host 控制的自动学习，与手动 Reflection 共用 `_ask()`、`_evidence()`、`_verify()`、`reflection.memory.plan_item()` 和 `reflection/writer.py`，没有新 Agent Loop。`learning/models.py` 限制来源、候选、目标文件、请求输入/输出和超时。`storage/learning.py::LearningRepository` 保存独立任务和来源处理记录，schema 11 追加对象，原迁移 1–10 不改。Markdown 是知识权威源；SQLite 只保存引用、哈希、状态及安全计量，不保存知识正文。

## 实际执行路径

1. Host 调用 `learn(run_ids=...)` 或 `learn(episode_ids=...)`。`snapshot()` 重新 JOIN Agent/Conversation/Run，拒绝非 completed Run；Episode 必须 finalized。先查询数量/字节预算，再通过原 `_source()` 加载完整 Run。跨 Conversation 的来源分别携带真实归属、started_at/ended_at，并提供独立处理时间 as_of；按真实来源开始时间排序，相对时间不能按处理日期猜测。摘要只是定位提示。
2. `create()` 以来源 ID/revision 去重，`_execute()` 先保存 processing。`propose()` 直接 Extraction，允许空列表，不强制 Worthiness。`_verify()` 对每个引用复用原精确引文校验，confirmed 必须有真实用户消息，不能仅引用 Assistant 或 Tool。
3. Selection 返回每个目标及 candidate_ids，可另外选有界相关文件进行对照。Host 要求候选完整覆盖、路径唯一、有界；只读真实 catalogue 中的现有路径。拒绝 persona.md 和索引直接写入。
4. 每个目标一次 Consolidation，按实际来源时间排序所有候选，更新当前认识、替换明确过时状态，重复/不确定结果可 no-op/review。`plan_item()` 校验唯一 old_text、expected_hash、大小、Frontmatter 和主题冲突。所有目标计划完成后才开始写入。
5. 再次校验原始来源哈希及 Episode revision；写入前保存仅含路径、前后哈希的回执。`write_item()` 同步复用 MemoryFS/index.create/index.edit，短锁、Git 隐私、Agent 路径检查和原子文件发布全部保留。每个目标最多一次写入，目标之间让出事件循环。
6. 文件成功后记录回执，全部终结后任务 completed。空提取、全 no-op 同样完成来源；不修改 Reflection、Episode、Thread 的独立进度。

## 异常与边界

取消模型等待恢复 pending，不推进来源；已准备/发布文件的任务保守进入 requires_reconciliation。重启 `recover()` 将无回执 processing 恢复 pending，有回执则等待核对。文件成功而记账失败不得宣称回滚。`reconcile(job_id)` 只读比较 after_hash 与索引可发现性，全部核实才 completed，不能核实则保持不确定，不盲目重发 create/update。索引部分成功使用原 /memory repair 补链后再核对。失败任务需显式 retry；persona 不受自动服务修改，Main Agent 没有获得新写权限。

Host 校验结构、真实引文与写入边界，不能数学证明模型的语义判断。假设、外部指令、未接受建议、敏感推测和冲突应由 Extraction/Consolidation 拒绝或标为不确定；原始来源沿用既有脱敏，响应及待发布片段做保守凭据模式检查，不宣称完备秘密识别。FakeProvider 测试不代表真实语义质量。

## 双速自动化的实际路径

`main._run_session()` 装配一个 Learner 并传给 `OrganizationRuntime`。首次 enabled 时 `activate()` 保存 Run/Episode rowid 水位；旧数据不静默重复学习，水位跨重启保留。`poll()/harvest_priority()` 每次最多扫描 16 个新 Run 的首条用户文本，用保守的记住/偏好/正式决定/明确更正等信号入队，不发分类 LLM。processing/failed/空结果与 completed 分开记录。

`learn_priority()` 尽快在回复之后的后台执行；`learn_batch()` 只在 Idle 处理 finalized Episodes，默认 3 个或最早来源等待 21600 秒。它们最终复用同一 `_execute()`，不复用 Reflection 进度。Priority 完成不会跳过包含其他 Run 的整个 Episode；Batch 再次读取现有 Markdown，由 Consolidation 判定重复 no-op。一次任务失败需要显式 retry，取消在没有文件回执时恢复 pending。

Runtime 统一包装 Episode/Thread/Learning 的 Provider，维护轮共享 4 批、4 请求、131072 保守预留 tokens、120 秒；批间让出、60 秒冷却。Dirty Repair 优先，学习与其他维护交替获得优先机会，每轮最多一个学习流水线。开始付费提取前保守检查整个流水线能否放入剩余请求/token 预算，避免预算不足反复支付 Extraction。自定义预算无法容纳时 status 的 learning_last_report 显示 learning_budget_config，保留待处理工作而不调用模型。

P2 enabled 时旧 MemoryAutomation 不再自动消费新 Run，旧计划、审批与数据全部保留；`/reflect` 仍可手动调试。关闭 AGENT_MEMORY_LEARNING_ENABLED 后才恢复原 Automation 模式；不同时自动执行两者。`/memory learning status|run|retry|reconcile <job_id>` 是可信调试入口，run/retry 使用共享预算和整轮超时；不向 Main Agent 注册自动写工具。原 /memory repair 可先补齐部分失败索引，再进行只读 reconcile。

存在 requires_reconciliation 时，自动学习暂停，防止新的文件更新破坏已保存回执的核对依据。所有配置在 `.env.example`；自动任务不主动打印正文。`tests/test_learning_runtime.py` 覆盖首启水位、Priority→Batch 不漏其他 Run、单 Episode 六小时机会、抢占、旧 Automation 不执行、空提取推进、失败显式重试及共享预算。FakeClock 不等待真实数小时。

## 阅读与验证

最小入口：`await learner.learn(run_ids=[completed_run_id])`；`tests/test_learning.py` 使用隔离 MemoryFS/SQLite，覆盖偏好更新、同文件合并、跨 Conversation、空提取/no-op、禁止路径/Persona、来源伪造、取消与失败显式重试，以及文件成功/SQLite 失败后只读核对。原 `tests/test_reflection.py`、`tests/test_automation.py` 验证审批和旧记录保持兼容。
