# SQLite 会话存储

当前 schema 11 追加 `storage/learning.py::LearningRepository` 的 memory_learning_jobs、memory_learning_sources、memory_learning_state；原迁移 1–10 定义不变。只保存任务状态、来源 ID/hash、文件发布回执、计量和首次启用水位，不保存长期知识正文或新事实数据库。Run/Message/Episode/Thread/Reflection 原位保留，initialize() 顺序原子升级。snapshot() 复核 Agent 与真实来源，save() 做状态/更新时间 CAS。跨文件/SQLite 无共同事务，processing 在重启分为 pending 或 requires_reconciliation，行为及原子迁移测试见 [Learning](learning.md)、[P2 交付](../p2-delivery.md)。

schema 8 的 `storage/maintenance.py::CONSISTENCY_SCHEMA` 增加 Thread source_revision、关联/语义变更失效触发器、重建检查点和归一化来源表。1–7 定义原样保留，Journal 不改；事务路径及验证见 [Thread Repair](thread-repair.md)。

schema 7 的 `storage/threads.py::ThreadRepository` 追加独立 Thread 状态、版本及批次审计，原子更新 Episode 关联和待更新标志。schema 6 的 Thread 外键插入/更新触发器复核同 Agent 归属。组织层只改派生表，不重写 Journal。

schema 6 仅追加 Episode、版本、归一化 Run 关联、独立进度/skip，以及预留真实外键所需的 topic_threads。`storage/episodes.py::EpisodeRepository` 负责有界来源和 CAS 短事务；原 1–5 迁移不改，所有旧 Conversation/Message/Run/Summary/Reflection 原位保留。见 [Organization](organization.md)。

P1 的 `SQLiteStore.last_interaction()` 按当前流查最近一个非 running Run 的开始时间，`LIMIT 1`，不加载消息正文。供宿主时间上下文计算互动间隔；不会因午夜或长间隔修改 Run/Conversation 归属。

先看[整体架构与 Pipeline](../architecture.md)的请求时序和事务表，再对照本模块的方法。

## 架构组成

`storage/sqlite.py::SQLiteStore` 使用标准库 sqlite3 和短事务；`storage/models.py` 定义记录模型。 `storage/lock.py::DatabaseLock` 复用 `file_lock.py::FileLock`，用 Windows msvcrt / POSIX flock 持有进程占用锁，数据库关闭或进程退出后由 OS 释放。锁文件可以保留，存在本身不代表被占用。 `storage/history.py` 是面向 ContextBuilder 的小型适配层。

表关系：Agent 1 → N Conversation，Conversation 1 → N Run / Message，Run 1 → N Message。 client_state 按 Agent 保存上次选中的 Conversation；复合外键防止选入其他 Agent 的会话。消息保存完整 JSON，包括 provider_data，并用会话内唯一递增 sequence 排序。当前数据库 schema 11 与发行包版本 0.7.2 是不同概念。

## 表字段与一致性约束

| 表 | 主要字段 | 设计作用 |
| --- | --- | --- |
| agents | id、name、system_prompt、created_at | 持久身份与兼容提示词元数据，V0.3 不将它加载为 Base，不保存每次运行中的消息状态 |
| conversations | id、agent_id、title、created_at、updated_at | 按 Agent 归属隔离会话，正常 Run 完成时更新时间 |
| runs | id、conversation_id、status、started_at、ended_at、result_json | 记录每次输入的执行生命周期，区别正常结束与中断 |
| messages | id、conversation_id、run_id、sequence、payload、created_at | payload 保存完整内部 Message；sequence 决定会话内重放顺序 |
| client_state | agent_id、conversation_id | 每个 Agent 的上次会话选择，与消息内容分开保存 |
| context_runs | run_id、conversation_id、首尾 sequence、message_count、payload_bytes | 已完成可重放 Run 的派生范围/大小索引 |
| context_summaries | conversation_id、text、through_sequence、through_run_id、revision、updated_at、checksum | 每会话当前工作摘要及其原子历史边界 |

messages 的 `(conversation_id, sequence)` 唯一；`(run_id, conversation_id)` 复合外键保证消息所指 Run 属于同一个会话。runs 的部分唯一索引只约束 status=running 的记录，允许一个会话有多个已结束 Run，但不允许两个同时运行。client_state 的复合外键避免跨 Agent 选择。 SQL 值都通过参数绑定传入；动态 schema 语句仅使用程序定义的对象名及内部顺序整数版本号。

payload 由 `TypeAdapter(Message)` 序列化，不拆成一张只有 role/text 的聊天表。这样原始参数字符串、工具错误、JSON 数据、reasoning 等 Provider 项目可以一起恢复。读取时重新校验内部模型，重放时再校验 Run 内调用配对；坏数据会明确报错，不被静默丢弃。

## 实际运行路径与事务边界

1. `run_cli()` 构造 SQLiteStore，解析绝对路径、创建目录、取得占用锁，打开连接并启用外键。
2. 显式调用 `initialize()`，在 BEGIN IMMEDIATE 事务内读 `PRAGMA user_version`，顺序执行 MIGRATIONS，最后更新版本。版本过新或无版本的陌生数据库均拒绝覆盖。迁移失败回滚表结构和版本。未来新增迁移，不修改已经发布的迁移定义。
3. `recover_incomplete_runs()` 在占用锁下把 running 标记 interrupted，保留消息，不执行任何工具。新建会话与更新 active 选择使用同一事务，选择写入失败时不留下额外空会话。
4. `begin_run()` 在同一事务内插入 running Run 和初始用户消息；部分写入失败则全部回滚。
5. `append_message()` 在事务内重新读取该 Run 的消息，验证调用/结果关系，追加一条消息；schema 2 的触发器在同一事务中更新派生 Recall token。模型工具调用在执行前提交；每个结果在下一个工具或模型请求前提交。
6. `finish_run()` 验证无悬空调用且以 assistant 消息结束，在一个事务内写终态、结果、结束时间，更新会话时间。只有正常 finalized Run 才进入可重放历史；schema 3 的 context_run_finished 触发器在同一事务更新该 Run 的有界加载元数据。
7. `interrupt_run()` 标记 cancelled / interrupted，不改写已提交消息。
8. 完整审计/显式调用 `load_messages(replayable_only=True)` 过滤 completed / failed，并再次验证每个 Run 的结构，用 Pydantic 恢复完整内部对象，Provider 随后能够原样重放 JSON 数据。

Run 状态：running；成功结束为 completed；有完整停止说明的模型错误、超时、轮数限制为 failed；用户取消为 cancelled；异常存储失败或下次启动发现遗留运行则为 interrupted。非成功 Run 的 result 字段可保留 RunResult；cancelled / interrupted 没有正常完成结果。

## 异常与边界

所有 SQL 值使用参数绑定，数据库错误转换为不含 SQL/内容的 HistoryError；写失败回滚当前短事务。单数据库同时仅支持一个应用进程，仍用唯一索引拒绝同一 Conversation 的重叠 running Run。当前在事件循环中同步执行短 SQLite 操作，繁忙等待上限 2 秒；没有异步连接池或外部数据库。数据库事务无法与外部工具副作用原子提交：工具完成而结果写入失败时，该调用完成状态未知。所有中断调用都不自动重试，不补造工具结果；未完成 Run 全部排除出模型上下文。本地数据库是明文私有数据，应备份并保留在被 Git 忽略的位置；没有自动备份或加密机制。

## 与 Markdown Memory 的提交边界

Core Memory 继续不放进聊天表；V0.4 schema 2 只增加 Recall 派生对象。SessionManager.context 使用程序 Base， agents.system_prompt 保留原值但不作为执行规则。MemoryFS 通过 Agent ID 在外部目录隔离。记忆单文件原子发布和 SQLite ToolResult 追加没有共同事务；前者成功、后者失败时，记忆保留已写入的新内容，Run interrupted，不自动撤销/重试。对应 tests/test_memory_runtime.py 的双存储失败测试；文件发布见[记忆模块](memory.md)。

## 示例与验证

使用 `with SQLiteStore(path) as store: store.initialize()` 显式建库/迁移。 `load_messages(conversation_id)` 读取完整审计；`list_runs(conversation_id)` 查看运行状态。

运行 `uv run pytest tests/test_storage.py tests/test_persistent_runtime.py`，覆盖迁移回滚、初始化幂等、锁释放、会话隔离、消息重载、结构化 Provider 数据、写入失败及真实进程退出。


## V0.4 派生 Recall 索引与原始历史

`storage/recall.py::INDEX_SCHEMA` 是 MIGRATIONS[2]：recall_text 是从 messages 投影 user / assistant text 的 VIEW，recall_fts 使用它作为 external content；原始 Provider 数据和工具结构不进入索引。INSERT / DELETE / UPDATE 触发器在原消息事务中维护 token；initialize() 用 rebuild 命令原子回填历史。没有另一份可编辑的 Recall 正文表。

`search_recall()` / `get_recall()` 在现有短事务内调用 storage/recall.py 的固定参数化查询，同时安装并清理 SQLite progress handler。所有结果 JOIN conversations 与 runs，验证宿主 agent_id 和 completed / failed 终态；搜索片段和展开文本都从 messages.payload 读取，索引只做定位。读接口不修改原历史，但沿用单进程拥有数据库及 BEGIN IMMEDIATE 的锁定策略，不承诺并发只读服务。

`rebuild_recall_index()` 只在可信宿主中显式调用，在一个事务内重建派生对象，原 Message ID/sequence/Run 不变；失败回滚全部重建步骤。缺失索引会使相关触发器写入失败，系统不会绕过这种一致性检查，先显式修复再继续。索引 shadow tables 在同一个私有 SQLite 文件里，不输出为可上传文件。中文取舍、大小限制与修复示例见[Recall](recall.md)，测试见 tests/test_recall_storage.py。

## V0.6 有界工作历史与 Summary 事务

`storage/context.py::SCHEMA` 是 MIGRATIONS[3]，追加 context_runs/context_summaries 和查询索引，迁移原子回填旧 completed/failed Run 的首尾 sequence、消息数与字节数。没有删除、重建或改写原始消息表，也不改变 Recall 对原消息的依赖。未知/更高版本和迁移失败处理仍如前。

正常调用路径：`ContextBuilder._load()` → `SQLiteConversationHistory.summary()/page()` → `SQLiteStore.context_summary()/context_page()` → storage/context.py。page() 从 conversation/sequence 联合索引选最多 max_runs+1 条元数据，先验证大小和数量上限，再按 run_id/sequence 索引读取有界完整消息并验证工具配对。不会先执行 load_messages() 全量反序列化后截断；大于一页的单个 Run 返回明确 ContextError。

`save_context_summary()` 在 BEGIN IMMEDIATE 短事务内调用 save_summary()：验证旧 checkpoint 的完整值，重新读取/比较仅本批来源的 Run、边界及内容 hash，确认它是旧边界之后下一段可重放前缀，再将摘要和边界/revision/checksum 一起 UPSERT。失败全部回滚该批次，之前独立成功的批次保留。源不可包含 running/cancelled/interrupted 或其他 Conversation，不能跳过正常完整 Run。读取也校验摘要 hash 与其已结束 Run 边界。

Summary 不作为 StoredMessage，不进入 Recall；完整审计 API 仍返回原值。SessionManager.resume() 只验证身份/摘要，实际页在发送前校验；status() 走 run_statistics()，不加载所有 RunResult。当前 Run 的 append/finish 仍校验该 Run 的完整消息，不会全量读取其他 Run。

SQLite 表本身假设由应用追加使用，外部直接篡改已覆盖的原始历史不通过每次全库校验发现；checksum 也不是防本机攻击的安全机制。单页最多 2 MiB、每 Run 最多 2048 条，SQL/I/O 仍同步执行。摘要表和数据库同为私人明文，应沿用已有备份和 Git 忽略保护。

最小宿主读取为 `SQLiteConversationHistory(store, conversation_id).page(after_sequence)`，完整示例和错误恢复见[Compaction](compaction.md)。`tests/test_compaction.py` 验证 schema 2→3 回填/回滚、来源与修订冲突、原子摘要写入、取消/进程退出、SQL sequence 范围和解码数量；`tests/test_recall_storage.py` 继续验证原始历史索引不受影响。

## V0.7 Reflection 审阅记录

`MIGRATIONS[4]` 引用 `storage/reflection.py::SCHEMA`，追加 reflection_progress、reflection_plans 和每 Conversation 一个活动计划的唯一索引，不改旧 Journal/Recall/Compaction。ReflectionRepository 绑定 agent_id，每次查询先验证 Conversation 归属；window() 复用 context.page(completed_only=True)，默认最多 8 个完整 Run，仍先检查字节/数量再解析。

新计划保存前，在短事务内验证独立进度和本批来源 Run/hash 未变；计划用有界 JSON、revision 与 checksum 保存。后续 save() 用 expected_revision 做 CAS；全部条目已应用/拒绝/跳过时，关闭计划和审阅进度同事务提交。空候选也保存关闭记录。字段、来源与批准状态见[Reflection](reflection.md)。

应用先保存 approved/applying，再调用现有 MemoryFS，结果单独提交。数据库失败不能回滚 Markdown，也不能把已写内容说成未发生。恢复时仍为 applying/partial 的操作不重写，显式 reconcile 仅核对并记账。测试 `tests/test_reflection.py` 覆盖 schema 3→4 回滚、源变更、CAS、审批写库失败、文件成功后写库失败及真实进程退出。


## V0.7.2 Phase 1 集成

schema 5 追加 storage/automation.py 的 automation_batches 与范围/活动唯一索引。绑定 Agent 检查归属；成功负判断和 reflection_progress 同事务提交，未完成或失败不能推进。原计划、原日志、Recall 与 Compaction 表不改写；已保存计划的 batch 关联可按源范围补记，不重复执行。见[Automation](automation.md)。
