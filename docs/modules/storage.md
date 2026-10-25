# SQLite 会话存储

先看[整体架构与 Pipeline](../architecture.md)的请求时序和事务表，再对照本模块的方法。

## 架构组成

`storage/sqlite.py::SQLiteStore` 使用标准库 sqlite3 和短事务；`storage/models.py` 定义记录模型。
`storage/lock.py::DatabaseLock` 用 Windows msvcrt / POSIX flock 持有进程占用锁，
数据库关闭或进程退出后由 OS 释放。锁文件可以保留，存在本身不代表被占用。
`storage/history.py` 是面向 ContextBuilder 的小型适配层。

表关系：Agent 1 → N Conversation，Conversation 1 → N Run / Message，Run 1 → N Message。
client_state 按 Agent 保存上次选中的 Conversation；复合外键防止选入其他 Agent 的会话。
消息保存完整 JSON，包括 provider_data，并用会话内唯一递增 sequence 排序。
数据库 schema 版本 1 与应用版本 0.2 是不同概念。

## 表字段与一致性约束

| 表 | 主要字段 | 设计作用 |
| --- | --- | --- |
| agents | id、name、system_prompt、created_at | 持久身份及共享提示词，不保存每次运行中的消息状态 |
| conversations | id、agent_id、title、created_at、updated_at | 按 Agent 归属隔离会话，正常 Run 完成时更新时间 |
| runs | id、conversation_id、status、started_at、ended_at、result_json | 记录每次输入的执行生命周期，区别正常结束与中断 |
| messages | id、conversation_id、run_id、sequence、payload、created_at | payload 保存完整内部 Message；sequence 决定会话内重放顺序 |
| client_state | agent_id、conversation_id | 每个 Agent 的上次会话选择，与消息内容分开保存 |

messages 的 `(conversation_id, sequence)` 唯一；`(run_id, conversation_id)` 复合外键保证
消息所指 Run 属于同一个会话。runs 的部分唯一索引只约束 status=running 的记录，
允许一个会话有多个已结束 Run，但不允许两个同时运行。client_state 的复合外键避免跨 Agent 选择。
SQL 值都通过参数绑定传入；唯一动态迁移语句中的版本号来自内部顺序整数。

payload 由 `TypeAdapter(Message)` 序列化，不拆成一张只有 role/text 的聊天表。
这样原始参数字符串、工具错误、JSON 数据、reasoning 等 Provider 项目可以一起恢复。
读取时重新校验内部模型，重放时再校验 Run 内调用配对；坏数据会明确报错，不被静默丢弃。

## 实际运行路径与事务边界

1. `run_cli()` 构造 SQLiteStore，解析绝对路径、创建目录、取得占用锁，打开连接并启用外键。
2. 显式调用 `initialize()`，在 BEGIN IMMEDIATE 事务内读 `PRAGMA user_version`，
   顺序执行 MIGRATIONS，最后更新版本。版本过新或无版本的陌生数据库均拒绝覆盖。
   迁移失败回滚表结构和版本。未来新增迁移，不修改已经发布的迁移定义。
3. `recover_incomplete_runs()` 在占用锁下把 running 标记 interrupted，保留消息，不执行任何工具。
   新建会话与更新 active 选择使用同一事务，选择写入失败时不留下额外空会话。
4. `begin_run()` 在同一事务内插入 running Run 和初始用户消息；部分写入失败则全部回滚。
5. `append_message()` 在事务内重新读取该 Run 的消息，验证调用/结果关系，追加一条消息。
   模型工具调用在执行前提交；每个结果在下一个工具或模型请求前提交。
6. `finish_run()` 验证无悬空调用且以 assistant 消息结束，在一个事务内写终态、结果、结束时间，
   更新会话时间。只有正常 finalized Run 才进入可重放历史。
7. `interrupt_run()` 标记 cancelled / interrupted，不改写已提交消息。
8. `load_messages(replayable_only=True)` 过滤 completed / failed，并再次验证每个 Run 的结构，
   用 Pydantic 恢复完整内部对象，Provider 随后能够原样重放 JSON 数据。

Run 状态：running；成功结束为 completed；有完整停止说明的模型错误、超时、轮数限制为 failed；
用户取消为 cancelled；异常存储失败或下次启动发现遗留运行则为 interrupted。
非成功 Run 的 result 字段可保留 RunResult；cancelled / interrupted 没有正常完成结果。

## 异常与边界

所有 SQL 值使用参数绑定，数据库错误转换为不含 SQL/内容的 HistoryError；写失败回滚当前短事务。
单数据库同时仅支持一个应用进程，仍用唯一索引拒绝同一 Conversation 的重叠 running Run。
V0.2 在事件循环中同步执行短 SQLite 操作，繁忙等待上限 2 秒；没有异步连接池或外部数据库。
数据库事务无法与外部工具副作用原子提交：工具完成而结果写入失败时，该调用完成状态未知。
所有中断调用都不自动重试，不补造工具结果；未完成 Run 全部排除出模型上下文。
本地数据库是明文私有数据，应备份并保留在被 Git 忽略的位置；没有自动备份或加密机制。

## 示例与验证

使用 `with SQLiteStore(path) as store: store.initialize()` 显式建库/迁移。
`load_messages(conversation_id)` 读取完整审计；`list_runs(conversation_id)` 查看运行状态。

运行 `uv run pytest tests/test_storage.py tests/test_persistent_runtime.py`，覆盖迁移回滚、
初始化幂等、锁释放、会话隔离、消息重载、结构化 Provider 数据、写入失败及真实进程退出。
