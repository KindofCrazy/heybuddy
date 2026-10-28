# P2 Memory Curator

P2 Review Fix：恢复后的 pending Curator 读取原 sources.memory 与已核实发布的目的文件，保留同一 Job 后重新规划；不重新 CREATE 已保存的 split 目的。当前比较依然受文件/字节限制并排除 persona/index。Learner 的启动/Idle 只读核对和真正冲突保护见 [Learning](learning.md)，tests/test_curator.py 的 partial split 验证补链后从当前文件继续。

## 职责与组成

`learning/curator.py::MemoryCurator` 在一小组 Markdown 上执行去重、证据支持的状态核对和有限组织。`CuratorPolicy` 默认 86400 秒检查、累计 12 次文件更新或选中文件超过 12000 字节才请求模型，最多三个文件、65536 输入字节。它使用 Learner 的同一锁、`ReflectionService._ask()`、`plan_item()`、`write_item()`、哈希回执和 reconcile，不维护另一个文件操作系统或事实数据库。

## 实际运行路径

1. `main._run_session()` 装配 Curator，Runtime 在 Idle 且没有未核对写入时调用 `due()`。到期但变化不够只顺延检查，不调用模型；失败需显式 retry。SQLite schema 11 已有 curator_due_at/curator_updates，不改旧迁移。
2. _files() 读取有界 catalogue，优先以最近修改路径选择至多两个相关 topic，Core human 可作对照；persona 与索引不作为写入对象。_evidence(paths) 通过 LearningRepository.related_sources() 对每个所选路径查找最近四个实际 applied 写入的 completed Learning Job，SQL 仅关联 operations.path 与 sources，不扫描 Markdown 历史正文。最多三个目标、每目标八个较新引用、合计八个唯一 Run，按目标轮流分配，避免一个文件吞掉所有证据。查询有原 SQLite VM/时间预算，非 IO 硬超时。每个来源用 snapshot() 重查 Agent、completed 状态、实际来源 hash/Conversation 和完整 Run；累计字节余量在读取 payload 前检查，坏来源不被采用，也不能让旧来源替代无法核实的新事实。

目标的 evidence_by_path 和 verified/insufficient/unavailable 状态随请求提供；实际原始 Run 按来源时间排序，较新相关更正在真实时间线上表达，而不是按最近两个全局任务取证。reconcile 变更只接受属于该路径关联 Job 的已验证 Run，仍必须有精确用户引文；即使别的被选文件有有效来源，也不能借来修改此路径。缺少可靠来源可做保守逐行结构整理或 no-op，不能从 Markdown 自行确认用户状态。没有额外分类或 Recall/Thread 检索模型调用，当前没有实现第二层 Episode/Thread fallback。
3. `run()` 在共享学习锁内生成一个 Curation 请求。已处理文件哈希与回执相同则直接 no-op，不再次付费。`_validate()` 要求唯一目标、限定文件集合和操作数。结构编辑保守校验旧事实行在最终文件集合中仍存在；事实变更必须引用真实用户消息，复用 Learner 精确引文校验。不确定冲突允许空 changes。
4. Split 只允许逐字复制一个真实 source_text 到新 topic；旧文件更新必须链接新路径，正文不得丢失。过长主题单独选择并预留新目标名额。所有 creates 排在 updates 前，目标文件和完整索引成功后才执行旧文件替换；目标因同名主题冲突降为 review 时拒绝整个计划，不能只执行后续来源移除。创建或索引失败立即停下，旧内容保留。
5. Learner `_execute(..., proposer=...)` 在首写前重新检查所有源文件哈希，保存无正文回执，再用原 MemoryFS/index 发布。实际使用的 Run ID/hash 保存为任务来源并在发布前复查，不占用 Priority/Batch 的来源处理记录。每个旧文件的 expected_hash 继续检查外部编辑。完成后推进 Curator 独立 deadline 和变更计数；不改 Reflection/Run/Episode 进度。

## 异常与恢复

取消等待模型时任务恢复 pending；文件已准备或部分发布时 requires_reconciliation，由 Learner 在启动/Idle 有界只读核对。安全状态自动回到同一 pending Job，按原文件和已存在目的重新规划；真正冲突/索引不完整继续保护，不能声称整体成功或删除副本。可用原 /memory repair 补链，再由 /memory learning reconcile 或 retry 核实；第三种 hash 不能盲目重试。/memory curator retry 使用当前文件和相关真实证据生成新计划，不重发旧 CREATE。

结构整理采用逐行保守约束，因此不支持自由改写所有段落；这降低误删风险。语义 reconciliation 仍依赖真实模型判断，Host 校验证明引文存在，不能保证推理正确。不会因时间久未提及删除稳定偏好，不执行整文件删除，不修改 persona，不实现全库扫描重写。

## 阅读与验证

可信调试入口 `/memory curator status|run|retry`，自动执行与 Episode/Thread/Learning 共享维护预算。`tests/test_curator.py` 覆盖重复事实、明确新用户证据、旧稳定偏好、未决冲突、目标先发布/索引后再移除原文、索引失败后安全重试、拒绝 Persona/删除/内容丢失/无来源更正、未变文件 no-op 和低频无工作不调用模型。
