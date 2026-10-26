# HeyBuddy V0.5：整体架构与 Pipeline

本文串起从启动到退出的真实执行路径，解释每一层接收什么、改变什么状态、在哪里保存数据。具体方法细节可从[文档索引](README.md)进入。当前系统是一个本地进程、一个 SQLite 数据库和独立的 Markdown 记忆目录，没有消息队列、后台任务消费者或自动续跑器。

## 1. 先区分实体、执行器和执行次数

| 概念           | 当前实现                               | 生命周期与职责                                                                    |
| -------------- | -------------------------------------- | --------------------------------------------------------------------------------- |
| 持久 Agent     | `storage/models.py::AgentRecord`     | 数据库中的身份、名称；system_prompt 保留为兼容元数据；CLI 默认 ID 为 `heybuddy` |
| Conversation   | `ConversationRecord`                 | 属于一个 Agent；隔离聊天历史，退出进程后仍存在                                    |
| Message        | `StoredMessage` 包装内部 `Message` | 属于一个 Conversation 和一个 Run，保存完整结构及顺序                              |
| Run            | `RunRecord`                          | 一次普通用户输入的处理过程，包含零个或多个已收到的模型响应、工具结果与终态        |
| Runtime 执行器 | `core/agent.py::Agent`               | 进程内 Python 对象，执行手写循环；不是数据库中的 Agent 实体                       |
| round          | `_run()` 的一次循环                  | 一次模型请求；一个 Run 可以消耗多个 round                                         |

因此，“一个 Agent 有多个会话”说的是数据库实体关系；“Agent Loop”说的是 Runtime 的执行职责。 CLI 可以复用同一个 Runtime，切换它绑定的 ContextBuilder，持久 Agent 的身份不会因此改变。 `/new`、`/sessions`、`/resume`、`/status` 是管理命令，不创建聊天 Run。

数据关系如下；client_state 是恢复上次选择的元数据，不承担记忆存储职责：

```mermaid
erDiagram
    AGENT ||--o{ CONVERSATION : owns
    CONVERSATION ||--o{ RUN : contains
    CONVERSATION ||--o{ MESSAGE : orders
    RUN ||--o{ MESSAGE : records
    AGENT ||--o| CLIENT_STATE : remembers_selection
    CONVERSATION ||--o| CLIENT_STATE : selected_by
```

数据库里的 `client_state.agent_id` 是主键，一个 Agent 最多保存一个当前选择；复合外键还要求选中的 Conversation 属于同一个 Agent。图中的关联不表示复制聊天记录：会话切换只改变选择，消息仍属于原会话。

## 2. 模块分层与依赖方向

```mermaid
flowchart TD
    CLI["main.py：配置、装配、输入输出"] --> Sessions["SessionManager：身份和会话选择"]
    CLI --> Runtime["Agent：有界执行循环"]
    CLI --> Context["ContextBuilder：上下文和 Run 生命周期"]
    CLI --> Memory["MemoryFS：Core 与分层 Deferred Markdown"]
    Context --> Reader["CoreMemory Protocol"]
    Memory -. "实现" .-> Reader
    MemoryTools["受限 Memory Tools"] --> Memory
    Tools --> MemoryTools
    Tools --> Recall["Recall：绑定 Agent 的历史证据"]
    Recall --> Store
    Runtime --> Context
    Runtime --> Provider["Provider Protocol"]
    Runtime --> Tools["ToolRegistry：验证与执行"]
    Context --> History["ConversationHistory Protocol"]
    Adapter["SQLiteConversationHistory：绑定 conversation_id"] -. "实现" .-> History
    Adapter --> Store["SQLiteStore：迁移、事务、消息日志"]
    Sessions --> Store
    OpenAI["OpenAIProvider：Responses 转换"] -. "实现" .-> Provider
    Store --> DB[("本地 SQLite")]
```

CLI 是装配入口，知道使用哪个具体 Provider、存储和工具。Runtime 只调用小接口，不导入 sqlite3 或 SDK 响应类型。ContextBuilder 负责历史何时加载、消息何时记录、Run 何时结束；具体 SQL 则集中在 SQLiteStore。

这让“换一个入口”和“换存储实现”不必改写模型/工具循环。当前只实现 CLI、一个官方 SDK Provider 和 SQLite 适配器，没有为未来系统预建更多服务。

| 边界                | 输入                              | 输出               | 谁理解具体协议                              |
| ------------------- | --------------------------------- | ------------------ | ------------------------------------------- |
| CLI → Runtime      | 用户文本、本轮权限集合            | `RunResult`      | CLI 理解交互命令，Runtime 不理解 slash 命令 |
| Runtime → Provider | 内部消息、`ToolDefinition`      | `ModelResponse`  | OpenAIProvider 理解 Responses API           |
| Runtime → Registry | `ToolCall`                      | `ToolResult`     | Registry 理解参数模型与执行函数             |
| Context → History  | 用户消息、消息追加、结束/中断通知 | Run ID、可重放消息 | SQLiteConversationHistory 绑定会话          |
| History → Store    | 会话 ID、Run ID、完整消息         | 持久记录           | SQLiteStore 理解表、外键和事务              |

## 3. 启动 Pipeline：先恢复状态，再接收输入

入口为 `pyproject.toml` 的 `personal-agent = personal_agent.main:main`，也可执行 `python -m personal_agent.main`。

1. `main.py::main()` 解析命令行。`--help` 在此结束，不读取配置或打开数据库。
2. `config.py::load_settings()` 读取当前目录配置，环境变量优先；Pydantic 校验模型、密钥、超时及 API 地址。失败时报告字段并返回退出码 2。
3. `main()` 配置安全日志，通过 `asyncio.run(run_cli(settings))` 进入异步应用。
4. `SQLiteStore(settings.database_path)` 解析路径、创建必要目录、取得进程占用锁，再打开数据库。同一路径已被其他应用实例占用时失败，避免两个 CLI 同时处理同一份运行状态。
5. `SQLiteStore.initialize()` 显式迁移到 schema 2，FTS5 索引及旧文本回填与版本更新在同一事务内完成，遇到更新版本或陌生数据库时拒绝继续。
6. `recover_incomplete_runs()` 将遗留 running Run 标记为 interrupted。这个步骤只更新状态，不调用 Provider 或任何工具。它在占用锁下进行，因此不会把另一正常运行实例的 Run 当作遗留任务。
7. `SessionManager(store)` 读取或创建持久 Agent，读取 client_state；首次创建空会话，后续验证并恢复上次选择的会话。
8. `run_cli()` 构造 `MemoryFS.for_agent()`，初始化或迁移普通 Markdown，绑定到 SessionManager。新安装默认用户数据目录，已有数据库旁 `.memory` 目录沿用；可配置根目录，详见迁移说明。
9. `_run_session()` 注册受限 Memory Tools 与绑定 Agent 的只读 Recall Tools，再创建 AsyncOpenAI 客户端、OpenAIProvider、内置 ToolRegistry、与当前会话绑定的 ContextBuilder，以及 Runtime Agent，然后调用 `chat()`。

这里没有“启动就请求模型”的步骤。直到用户输入普通文本才进入请求 Pipeline。启动存储失败返回退出码 1，不悄悄退回内存模式；显式使用不带 history_store 的 ContextBuilder 仍可获得进程内模式，原有 Runtime 单元测试使用该方式。

## 4. 请求 Pipeline：输入 → 上下文 → 模型 → 工具 → 结果

### 4.1 一次调用工具的完整时序

下图把 history 适配器和 SQLiteStore 合并为“持久化历史”参与者，便于看清调用顺序；实际方法映射见[ContextBuilder](modules/context.md)和[SQLite 存储](modules/storage.md)。

```mermaid
sequenceDiagram
    participant CLI
    participant A as Agent
    participant C as ContextBuilder
    participant H as 持久化历史
    participant P as Provider
    participant T as ToolRegistry
    CLI->>A: run(user_input)
    A->>C: begin(user_input)
    C->>H: load 可重放历史
    H-->>C: completed / failed 消息
    C->>H: begin：保存 running Run 和用户消息
    C-->>A: system + 历史 + 新输入
    A->>C: for_request(messages)：编译最新 Core Memory
    A->>P: await generate(请求上下文, 已授权工具)
    P-->>A: ModelResponse(tool_calls)
    A->>C: record(AssistantMessage)
    C->>H: 保存调用，提交事务
    A->>T: await execute(call)
    T-->>A: ToolResult
    A->>C: record(ToolMessage)
    C->>H: 保存结果，提交事务
    A->>C: for_request(messages)：再次读取最新记忆
    A->>P: await generate(追加结果和最新记忆的上下文)
    P-->>A: ModelResponse(text)
    A->>C: record(最终 AssistantMessage)
    C->>H: 保存最终消息
    A->>C: commit(messages, RunResult)
    C->>H: finish：验证完整性，更新终态
    A-->>CLI: RunResult(completed)
```

请求路径中的实际操作按以下阶段执行：

| 阶段     | 实际方法                                                    | 输入/输出与状态变化                                             |
| -------- | ----------------------------------------------------------- | --------------------------------------------------------------- |
| 接收     | `chat()` → `Agent.run()`                               | 普通文本进入 Runtime；空白输入由 CLI 跳过，Runtime 也拒绝空输入 |
| 串行化   | `Agent.run()` 的 `asyncio.Lock`                         | 同一 Runtime 的 Run 顺序执行，避免上下文交错                    |
| 构建     | `ContextBuilder.begin()` → `build()`                   | system + 当前会话可重放历史 + 新用户消息                        |
| 开始记录 | `ConversationHistory.begin()` → `begin_run()`          | 原子保存 running Run 与用户消息，得到 Run ID                    |
| 请求模型 | `Agent._run()` → `Provider.generate()`                 | 在单次请求超时内 await，消耗一个 round                          |
| 响应检查 | `_run()` 的 ID/空响应检查                                 | 拒绝重复 ID 或既无文本也无调用的响应                            |
| 记录响应 | `_run()` 内部 `append()` → `ContextBuilder.record()` | 先落库，再加入 pending 和当前上下文                             |
| 执行工具 | `ToolRegistry.execute()`                                  | 名称查找、参数校验、执行与 JSON 规范化，生成同 ID 的结果        |
| 继续     | 再次 `append()`，进入下一轮                               | 先保存结果，再允许下一工具或模型请求                            |
| 完成     | `_run()` 内部 `finish()` → `ContextBuilder.commit()` | 保存正常终态，清理运行中状态，才返回 CLI                        |

### 4.2 哪些分支会继续，哪些会停止

只有“没有工具调用且文本非空”才作为最终答案。文本与工具调用同时出现时，先处理调用，这段文本不会提前作为最终成功展示。多个工具按响应给出的顺序逐个执行；每个结果保存成功后，才处理下一项。未知工具、参数错误、执行异常或工具超时都转为 ToolResult，模型可在下一轮处理错误。工具报错本身不必使整个 Run 成为 failed：模型仍可能解释错误并给出最终回答。

最后一轮不提供工具定义，保留给最终回答。如果模型仍返回调用，Runtime 还会在执行层再次拦截：为所有调用保存 `round_budget_exhausted` 结果，不进入 Registry，返回 max_rounds。因此 max_rounds=1 只允许直接回答；系统不会为总结再多发一个超预算请求。

### 4.3 用一条具体消息链跟踪数据

假设用户输入“计算 2 + 3”，模型先调用加法，再回答“5”，这是一个 Run、两个 round：

| 会话 sequence | 内部消息                   | 关键字段                                                        | 保存时 Run 状态                         |
| ------------- | -------------------------- | --------------------------------------------------------------- | --------------------------------------- |
| 1             | `TextMessage(role=user)` | `text=计算 2 + 3`                                             | running，与 Run 同一事务创建            |
| 2             | `AssistantMessage`       | `tool_calls[0].id=sum-1`，`name=add_numbers`，原始参数 JSON | running，工具尚未执行                   |
| 3             | `ToolMessage`            | `result.call_id=sum-1`，`ok=True`，`data={sum: 5}`        | running，工具已经返回                   |
| 4             | `AssistantMessage`       | 最终文本“5”，可能带 Provider output items                     | running，消息已保存但 Run 尚未 finalize |

随后 `finish_run()` 把状态改为 completed，并保存 RunResult、ended_at、会话 updated_at。 sequence 是会话内顺序；下一条用户消息从 5 开始。Run ID 用来区分每次输入， `ToolCall.id` 与 `ToolResult.call_id` 则用来关联同一次工具调用，两者不能互相替代。

同一条 assistant 消息可以携带多个调用，后面对应多条 tool 消息。一次 Provider 响应里的多个 reasoning、文本、函数项目保存在这条消息的 provider_data 内，不会各自变成数据库顶层消息。

## 5. 四种历史视图：已写入与可重放并非同一回事（请求编译见第 11 节）

| 视图           | 读取位置                                     | 包含内容                                    | 用途                           |
| -------------- | -------------------------------------------- | ------------------------------------------- | ------------------------------ |
| 完整持久化历史 | `SQLiteStore.load_messages()`              | 所有 Run 的消息，包括未完成调用             | 审计、诊断，不直接全量送入模型 |
| 可重放历史     | `ContextBuilder.history` → adapter.load() | 仅 completed / failed Run，且通过结构校验   | 构建下一次输入的上下文         |
| 工作消息链     | `_run()` 的局部 `messages`               | system、可重放历史、新输入、本 Run 后续消息 | for_request 编译前的基础链     |
| 运行中消息     | `ContextBuilder.pending_messages`          | 仅当前 Run 的用户、模型、工具消息副本       | 跟踪尚未结束的 Run             |

pending 消息可以已经逐条持久化；“Run 未完成”不表示持有一个一直不提交的 SQL 事务。例如正在执行工具时，调用已在数据库中，但整个 Run 仍是 running，尚未进入正常可重放历史。这种区分避免“数据库中有一条调用”被误解为“下次启动应继续执行它”。

Base system 提示词来自程序代码，不作为每个 Run 的重复数据库消息保存。当前对 Conversation 的可重放历史全部加载，不进行召回筛选、摘要或 token 压缩。

## 6. 持久化 Pipeline 与事务边界

`SQLiteStore._transaction()` 以 BEGIN IMMEDIATE 开始短事务，成功 commit，异常 rollback。不在等待模型或工具时保持数据库事务开放。各阶段的提交含义如下：

| 事务               | 写入内容                                                  | 失败后的含义                           |
| ------------------ | --------------------------------------------------------- | -------------------------------------- |
| schema 初始化/迁移 | 表、索引、user_version                                    | 全部回滚，避免半个 schema              |
| 新建并激活会话     | conversations + client_state                              | 全部回滚，不留下多余空会话             |
| 开始 Run           | runs + 首条用户消息                                       | 全部回滚，模型还未被调用               |
| 追加消息           | 校验既有 Run 消息，插入完整 JSON 及下一个 sequence        | 只回滚本次追加，保留此前日志           |
| 完成 Run           | 检查调用完整性，更新终态、result_json、结束时间与会话时间 | 全部回滚，不能报告最终成功             |
| 标记中断           | 更新 cancelled / interrupted 与结束时间                   | 写入仍失败时保留 running，待启动时处理 |

数据库约束防止同一会话的 sequence 重复、消息挂到其他会话的 Run，以及同一 Conversation 同时出现两个 running Run。`validate_turn()` 还检查调用 ID、工具名和结果的配对关系；正常结束必须没有悬空调用，最后一条是 assistant 消息。

这些事务保障的是数据库内部一致性。外部工具效果不属于 SQLite 事务， “工具执行成功 → 结果落库”之间的窗口不能被这套设计消除。

## 7. 失败 Pipeline 与 Run 状态

```mermaid
stateDiagram-v2
    [*] --> running: begin_run 提交成功
    running --> completed: 最终文本和完成状态已保存
    running --> failed: 正常停止记录已保存
    running --> cancelled: 用户取消且标记成功
    running --> interrupted: 存储故障且标记成功
    running --> interrupted: 新进程启动发现遗留运行
```

只有 completed / failed 进入下一次正常上下文。failed 表示“Run 已明确结束并完成保存”，不是“可能还要后台重试”。它可以对应 RunResult 中的 provider_error、timeout、invalid_response、 runtime_error（Provider 调用中的非预期异常）、memory_error 或 max_rounds。

| 失败位置                        | Runtime 行为                            | 保存/重放语义                             |
| ------------------------------- | --------------------------------------- | ----------------------------------------- |
| 用户消息或 Run 创建失败         | 返回 storage_error，rounds=0            | 不调用模型；当前创建事务回滚              |
| 模型请求超时或报错              | 保存停止说明，返回明确 RunResult        | 正常 finalize 为 failed，可重放           |
| assistant 调用记录写入失败      | 立即停止，不执行该批工具                | 尝试标记 interrupted，保留之前日志        |
| 工具执行失败                    | 保存对应错误 ToolResult，继续有限循环   | 有后续预算时交给模型处理                  |
| 工具已返回，结果写入失败        | 立即停止后续工具，不自动重试            | 工具效果可能已发生，当前 Run 排除出上下文 |
| 最终消息已保存，finish_run 失败 | 返回 storage_error，不展示最终成功      | 尝试标记 interrupted，已保存消息只作审计  |
| 用户取消当前运行                | 尝试标记 cancelled，传播 CancelledError | 部分消息保留，不自动执行剩余调用          |
| 进程直接退出                    | 已提交日志保留，Run 可能仍为 running    | 下次启动标记 interrupted，整个 Run 不重放 |

若连中断标记也无法写入，代码只记录安全诊断；Run 仍为 running，下次启动再标记。中断记录里的未配对调用可能尚未开始，也可能已经产生外部效果，不能根据缺少结果断言未执行。同步工具超时同样只停止等待，线程可能继续；详细契约见[工具模块](modules/tools.md)。

## 8. 会话切换 Pipeline

管理命令只在两次 Run 之间处理，当前没有后台 Run 与切换命令并发执行的路径。

- `/new`：`SessionManager.new()` → `create_conversation(activate=True)`，原子新建空会话并更新 client_state；成功后 `agent.context = sessions.context()`。
- `/resume <id>`：先查会话归属和可重放历史，再保存当前选择，最后替换 ContextBuilder。验证或保存失败时，旧选择不被修改。
- `/sessions`：只列当前持久 Agent 的会话，按 updated_at 降序、ID 排序；不创建聊天消息。
- `/status`：读取当前会话 Run 状态，提示中断/取消记录未进入模型上下文。

切换时 Provider、SDK 客户端、ToolRegistry 和 Runtime 对象继续复用，替换的是绑定会话的上下文。新会话使用程序 Base System Prompt 和同一个 Agent 的 Core Memory，但其历史为空，不会拷贝上一会话的 `_history`。切回旧 ID 时再从数据库加载，两个 Conversation 的 sequence 和 Run 各自独立。

## 9. 退出与重启 Pipeline

正常 exit / quit / EOF 时，`chat()` 返回，SDK 客户端和数据库依次关闭，数据库占用锁释放。下次启动从 client_state 找回上次选择，再通过 ContextBuilder 的历史接口重建消息。

重启不会把旧工具调用当作新的执行请求。已完成历史中的调用/结果只是 Provider 输入的一部分；只有模型在本次 Run 的新响应中发出调用，而且仍有执行预算时，Runtime 才进入 Registry。被中断的整个 Run 会排除出这条重放路径。

Provider 专用数据必须跟随消息保存：OpenAIProvider 把 output items 转成普通 JSON，放入 `provider_data['openai']`。重载时 Pydantic 恢复这个字段；`OpenAIProvider._input()` 直接重放原始项目及顺序，不再重复追加归一化调用。工具输出则按 call_id 转成 function_call_output。因此 reasoning、函数参数、调用 ID 和结果之间的关系可以跨进程保留。

这使“重启后追问前文”成为恢复 Conversation History 的结果。这与 Core Memory 独立：`/new` 后仍注入同一 Agent 已提交的记忆，但不会检索或复制旧会话历史。

## 10. 按 Pipeline 验证与阅读源码

| 路径                   | 建议先读的源码                                          | 对应测试                                                                    |
| ---------------------- | ------------------------------------------------------- | --------------------------------------------------------------------------- |
| 启动和迁移             | `main.py::run_cli`、`storage/sqlite.py::initialize` | `test_initialization_is_explicit_versioned_and_idempotent`                |
| 一次 Run 与工具循环    | `core/agent.py::run` / `_run`                       | `tests/test_agent.py`                                                     |
| 运行中记录与历史分离   | `core/context.py`、`storage/history.py`             | `test_pending_history_is_distinct_from_replayable_and_full_history`       |
| 会话隔离与多次重启     | `sessions.py`、`load_messages`                      | `test_restart_restores_previous_topic_and_new_conversation_is_empty`      |
| SDK 重放完整数据       | `llm/openai_provider.py::_input`                      | `test_complete_provider_items_replay_through_sdk_after_restart`           |
| 写入失败后的副作用边界 | `append_message`、`Agent._abort_context`            | `test_result_write_failure_stops_batch_and_keeps_prior_history`           |
| 真正进程退出           | `storage/lock.py`、`recover_incomplete_runs`        | `test_actual_process_exit_releases_lock_and_retains_unfinished_run`       |
| CLI 切换               | `main.py::chat`、`SessionManager.resume`            | `test_cli_session_commands_switch_and_restore_without_polluting_messages` |

上表源码路径以 `src/personal_agent/` 为根；存储测试位于 `tests/test_storage.py`，跨模块测试位于 `tests/test_persistent_runtime.py`。它们使用临时数据库、Fake Provider 和 MockTransport，可以在不使用真实 API Key、不支付模型费用的情况下观察整条 Pipeline。

## 11. V0.5：Base / Core / Deferred / Recall

程序 `DEFAULT_SYSTEM_PROMPT` 是可信 Base，SessionManager 不从旧 agents.system_prompt 恢复执行规则。ContextBuilder 只依赖 `CoreMemory.compile()`，每次请求自动注入三块 Core 的 JSON 数据；MEMORY.md 已包含顶层 Deferred 入口，不再额外拼接全部目录正文。

```text
ContextBuilder.begin → Base + 当前会话可重放历史 + 本次输入
Agent._run 每次 Provider.generate 前 → ContextBuilder.for_request
  system: 程序 Base
  user: JSON {kind: agent_core_memory, trust: data_only, blocks: ...}
  当前会话历史 + 本 Run 消息
模型按需 → deferred_read(目录 MEMORY.md) → deferred_list(元数据)
         → deferred_read(有关主题) → 正常 ToolMessage → 下一次请求
历史取证 → recall_search / recall_get → 正常 ToolMessage
```

Core 保存稳定人格、用户信息、重要原则和顶层地图。Deferred 保存按主题提炼的长期理解、取舍与开放问题；Recall 保留原始历史证据。三者不自动互相复制。目录 name/description 和概要帮助判断是否深入；未来请求通过 Core 根地图逐级发现，而不是依赖模型猜到已有文件名。

Core 编译上限默认 8192 UTF-8 字节，包含 JSON 开销；Deferred 新增正文不扩大这个预算。编译不写 pending/SQLite；工具结果正常审计保存。编译错误停止该次 Provider 请求，返回 memory_error。同 Run 写入后下一次请求读取最新 Core，新会话和重启同样共享最新文件，但不继承别的 Conversation 聊天。

## 12. 本轮授权和写入路径

```text
/memory edit 请求 → Agent.run(memory.read + memory.update + deferred.update)
  → Provider 读取当前内容/hash → 请求局部编辑或创建
  → 调用先落库 → Registry 复查权限与严格参数
  → MemoryFS.writing：路径/隐私检查 + 短 FileLock
  → edit 核对 expected_hash + old_text 精确唯一匹配
  → 验证文件和 Core 总容量/Frontmatter
  → 同目录临时文件 + flush/fsync → 再核对摘要 → os.replace
  → 返回新 hash → ToolResult 落库 → 下一轮模型请求
```

`/memory read` 只额外授权固定 Core 读取；普通对话默认提供 Deferred 列举、分段读取、索引检查和原有 Recall。所有写入都需要可信入口明确授权，且只持续当前 Run。自然语言、旧命令、记忆内容不能赋权。Core 使用固定 block，Deferred 使用 Agent 根内受限路径，模型没有 agent_id 参数。最终轮拒绝新工具的策略保持不变。

`memory_history`、`memory_restore` 和 `/memory restore` 已移除；没有 Revision、恢复授权或伪装的新历史接口。局部编辑优先，memory_update 仍是需用户明确请求的完整 Core 块替换。旧 expected_version 改为目标文件的 expected_hash。

## 13. 文件、索引和 SQLite 的边界

正常布局是 `<memory_root>/<agent_id>/{persona.md,human.md,MEMORY.md,topics/...}`，用户可直接编辑普通 Markdown。默认新数据在用户目录；已有数据库旁 `.memory` 目录原地继续使用。没有每次更新复制三块的成本，摘要也不保存旧版本。

单文件原子替换确保正常本地进程崩溃时读到旧或新完整内容；受控写入有短锁。编辑器不参与锁，摘要核对到替换之间仍有竞争窗口，不支持跨文件完整快照或断电保证。磁盘 I/O 和 Git 隐私检查仍同步执行，asyncio 超时不能强制中断，写入不留在后台线程。

`index.create()` 先创建主题，再由 `ensure_chain()` 创建缺失目录索引、逐级追加父链接。全部成功才返回 indexed=true；后续失败返回 memory_index_incomplete，明确保留已经发生的效果。`index.inspect()` 检查缺失/悬空/跳级/无效链接，`index.repair()` 只追加缺项，保留用户文字。根容量不足同样可能造成部分完成，需要检查后整理 Core；不偷偷提高限制或删除内容。

文件发布与 SQLite ToolResult 保存无共同事务。后者失败返回 storage_error 并中断 Run，文件可能已经成功，不撤销或自动重试。ToolMessage 不伪装用户消息；记忆文本始终是低信任数据，不能覆盖系统或工具权限。

## 14. V0.4 一次性迁移

`memory/migration.py::initialize_files()` 检测 CURRENT/versions，校验当前已提交版本的三个块和哈希，先建立唯一完整备份，再原子导出相同字节到根文件。`.migration-v05` 仅记录备份位置及是否完成；中断后从已固定的备份恢复导出，只补缺失且一致的目标，遇到人工编辑拒绝覆盖。成功后正常读写不再访问旧格式，不会删除旧目录或备份。

迁移前的校验/备份失败不更改源；导出中断保留源和备份。新文件已存在时不会静默覆盖，部分新格式也不会自动初始化抹掉原文。完整步骤与人工处理见[迁移说明](memory-migration.md)。只保留这段迁移读取代码，没有永久双存储、版本数据库或新历史系统。

## 15. 设计依据与验证路径

[Letta Code 源码调研](memory-design.md) 区分上游真实实现、V0.4 已有能力和本次取舍。采用分层 MEMORY.md、name/description、渐进读取与主题理解；不复制 Git-backed MemFS、system prompt learning、Reflection/worktree、远程同步或自动提取。

`tests/test_memory_store.py` 验证原子文件写、进程退出、编辑器更改、冲突、路径和容量；`test_memory_migration.py` 验证字节保留、完整备份、重复/中断迁移；`test_deferred_memory.py` 验证索引链、分页读取、部分失败/补链、CLI 新会话/重启和 Recall 协作；`test_memory_runtime.py` 保留 Runtime 授权与双存储窗口测试。全部离线，不调用真实模型。

## 16. V0.4 Conversation Recall Pipeline

Core Memory 是每次请求编译的稳定参考数据，Conversation History 是原始审计日志，Recall 是从该日志按需取得的证据；三者没有合并成同一存储或默认上下文。Agent Loop 和 Provider 继续使用原有工具机制。

```mermaid
flowchart LR
    Input[新会话询问历史决策] --> LLM[Provider 选择关键词]
    LLM --> Search[recall_search]
    Search --> Bound[Recall 绑定宿主 Agent ID]
    Bound --> SQL[SQLiteStore 搜索事务]
    SQL --> FTS[三字符以上 FTS5 trigram]
    SQL --> Short[一至两字符 有界 LIKE]
    FTS --> Source[原始 Message + Conversation + Run 校验]
    Short --> Source
    Source --> Hit[有限 snippet + 来源 ID + 序号]
    Hit --> Expand[recall_get 再次验证归属]
    Expand --> Answer[模型引用历史或说明无证据]
```

启动时 `SQLiteStore.initialize()` 在原子 schema 1→2 迁移中创建派生 view、FTS5 表、消息触发器和 Agent 归属索引，再回填旧 user / assistant 非空 text。回填不修改原始消息、Run、ID 或顺序。新消息的触发器与原写入共用事务；Run 在 finish_run 事务提交后才对查询可见。迁移失败保持原 schema 和历史，不将半成品索引作为可用数据源。

`main._run_session()` 以 `Recall(sessions.store, sessions.agent.id)` 装配只读工具，`agent_id` 不进入模型参数。普通输入不用额外授权，模型根据 Base 中的规则先搜索关键词、按需展开。原 Agent Loop 先持久化调用，再执行 Registry、保存关联 call_id 的结果并继续模型请求；终轮仍禁止新工具调用。ContextBuilder 没有自动全库检索，结果只按当前会话正常工具消息保存，不默认带进另一个新会话。

搜索只返回 completed / failed Run 的文本证据，运行中、取消或中断内容都不可检索，读取单条 ID 时再次做同样过滤。不会暴露 Provider 原始项目、reasoning、工具参数或 Tool Result，更不会因展开历史而执行旧调用。低信任的历史文本不能改变 Base、工具注册或 memory.update / deferred.update 授权；未找到记录不等于过去一定没有发生，模型应说明未找到证据和范围限制。

三个字符以上使用转义的字面 FTS 短语，中文不依赖分词；短词回退到最近 2000 条可见文本的有限前缀，返回 scope_limited。参数、snippet、展开窗口、总输出和 SQLite VM 工作均有上限，详细数值见[Recall 模块](modules/recall.md)。这提供字面关键词搜索，不提供语义检索、重排序或 Compaction。

索引候选必须回到 canonical messages 读取正文并再次核验字面条件，不能输出伪造的索引正文。缺失 token 可能造成假阴性，索引故障返回安全错误，可信宿主可显式调用 `SQLiteStore.rebuild_recall_index()` 在事务中重建派生对象；不会自动尝试任务恢复。SQLite 文件本身的损坏不属于派生索引重建可保证修复的范围。

对应 tests/test_recall_storage.py 的迁移、事务、中文查询、隔离、范围和重建测试，以及 tests/test_recall_runtime.py 的 Fake Provider 搜索→展开→回答、跨新会话不注入、历史不提权和旧副作用不重跑测试。
