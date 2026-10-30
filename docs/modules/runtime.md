# Shared Chat Runtime

`channels.napcat.runner.run_napcat()` 同样通过 `open_application()` 取得唯一 ChatService，并在小号身份核对后启动共享 QQ 消费者。只接收配置主号的好友消息，使用空 permissions；Luna、Memory、检索及维护服务仍在原 `assemble_chat()` 装配，不新增渠道角色或后台 Agent。实际路径与验证见 [NapCat](napcat.md)、`tests/test_napcat.py`。

assemble_chat() 向 create_builtin_registry 显式传入 local_time=True，使 CLI/QQ 的时间工具与 ContextBuilder 时间注入使用同一宿主本地时钟和显示格式；该选择不改变默认离线 Harness 的 UTC 契约。

生产工具装配关闭 add_numbers 演示，并启用 archival_memory_search 统一只读入口。CLI、QQ 和只读 Observatory 都沿用该装配，普通聊天通常提供四个工具。离线测试使用兼容注册表，不代表生产能力。tests/test_config_cli.py 和 test_qq_integration.py 检查实际装配的模型工具定义，tests/test_archival_memory.py 验证统一入口。

## 职责与架构组成

`src/personal_agent/runtime.py` 的 `open_application()` 持有 SQLite 单进程锁、SessionManager、MemoryFS 与 Provider 生命周期。`open_chat()` 管理 SDK 客户端；`assemble_chat()` 复用现有 ContextBuilder、工具注册、Reflection、P1 Organizer、P2 Learning/Curator/Adaptive Retrieval、P3 Character Package/State，返回轻量 `ChatService`。渠道不各自装配学习或角色服务。

## 代码实际运行路径

1. `main.run_cli()` → `open_application()`：初始化 schema，恢复遗留 Run 的审计状态，再恢复已选 Conversation；不会重试工具。
2. 初始化同一 Agent 的 MemoryFS，然后 `open_chat()` → `assemble_chat()` 加载角色、检索与普通工具，绑定现有 P1/P2/P3 服务。
3. CLI `main._chat()` 在本地解析管理命令；普通文本通过 `ChatService.run()` → `foreground()` 取消并等待后台维护，再调用原有 `Agent.run()`。权限仅来自可信 Host 参数。
4. 渠道展示回答后调用 `after_reply()`，沿用 `OrganizationRuntime.schedule()` 的空闲、累计数量和维护预算。它不逐条强制整理 Episode。
5. 退出时 CLI 先关闭自己的输入桥；open_chat 最终关闭后台任务，再关闭 SDK、SQLite 与占用锁。

QQ 入口 `channels.qq.runner.run_qq()` 也调用 `open_application()`，然后把同一 `ChatService` 交给 `QQAdapter`，只传净化后的文本和空权限。它不调用 `main._chat()` 的本地管理命令解析器。Luna 等 persona.json 角色只装配稳定提示词和历史角色标签，不创建 CharacterStateService；旧 Markdown 角色仍沿原路径恢复状态。P2 的 Learner、Curator、回执核对与 Adaptive Retrieval 也使用同一装配。`tests/test_qq_integration.py` 验证 CLI→退出→QQ 的历史、Memory、旧角色的 Character State、新 JSON 角色无状态服务，以及 QQ 抢占真实后台 Learning。

## 异常与边界

Agent 的失败继续返回 `RunResult`；初始化失败沿原 CLI 安全错误路径返回。SDK retries 固定为 0。文件与 SQLite 的提交窗口、Reflection 审批和 P2 回执核对不变。`Agent.run()` 的现有锁串行处理前台 Run；渠道负责限制输入积压和外部回复失败。单个数据库只允许一个本地应用进程。

## 阅读与验证

运行 `uv run personal-agent`。`tests/test_config_cli.py` 验证 CLI、SDK 关闭与会话装配；`tests/test_p2_cli.py` 验证唯一 Learning 入口、共享检索器及只读管理状态。离线测试用 Fake Provider，不连接真实模型。
# Observatory 接线

`ChatService.run` 在 try/finally 设置 `foreground_active`，取消或失败也会清除。`qq_status` 是 QQ runner 绑定的无参数读取 callback，仅返回 Gateway/consumer 生命周期枚举。HTTP 不调用这两个执行入口；它只读取标记与持久元数据。

`dashboard/app.py:create_app` 接受同一个 `ChatService`，只保留读取投影。QQ 的 `run_qq(..., dashboard=True)` 在 `open_application()` 内通过 `AsyncExitStack` 启动 HTTP，复用同一 SQLite 连接、Agent Scope 与 MemoryFS。独立 `web` 同样调用 `open_application()`，不安排后台维护；已有进程持有锁时拒绝启动。详情见 [dashboard.md](dashboard.md)，验证见 `tests/test_dashboard.py`。


## Observatory 只读装配

`assemble_chat`、`open_chat`、`open_application` 支持可信宿主参数 `observer_only=True`，仅独立 `web` 使用。复用原有数据库与服务装配，但不进行 Run/回执恢复、Memory 初始化/迁移、Learning 激活、Curator 调度或角色状态初始化，也不创建后台维护任务。`ChatService.run` 在此模式拒绝执行，`after_reply` 不调度。普通 CLI/QQ 默认行为不变。验证见 `tests/test_dashboard_lifecycle.py` 与 `tests/test_dashboard_boundaries.py`。

## 渠道无关生命周期

OrganizationRuntime 构造不创建 AsyncInput，QQ 与 observer-only Web 不占用终端线程。channels/lifecycle.py::stop_on_termination 供 QQ 与 Web 共用，Web 不再从 QQ runner 导入通用信号处理。生产 CLI 使用原 ChatService，不重新包裹而丢失宿主标记；维护与 Provider 的关闭 owner 为 open_chat。最小运行入口与测试仍为 run_cli、run_qq、run_web 和 tests/test_qq_integration.py/test_dashboard_lifecycle.py。

## 共享图片输入

`ChatService.run(..., images=...)` 将不可变图片引用传给同一 Agent。`open_chat()` 为 OpenAIProvider 注入绑定当前 Agent 的 SQLiteImages 读取器，不在 QQ Adapter 新建视觉模型/人格/学习器；包加载与维护配置不变。全路径及验证见[图片输入](images.md)。
# Proactive Shadow 接线（schema 16）

`assemble_chat()` 增加 `ProactiveRuntime`，复用 Provider/ContextBudget；启用时初始化增量水位，只读装配不写。`open_chat()` 在 Shadow 开启时启动现有维护任务，不创建独立模型循环。`ChatService.note_input()` 以及 QQ 收件后同步调用使等待中的 Attention 建议失效；`run()` 继续优先 await foreground() 抢占维护。`close()` 沿用维护任务退出，不产生主动投递。完整路径、配置和离线验证见 [proactive.md](proactive.md)。

# Composer 与离线投递装配

`assemble_chat()` 为当前 Agent 建立 OutboxRepository，在 Agent.context 构造前设置 SessionManager.notice_factory，并将 OutboundRuntime 接入 ProactiveRuntime.outbound。角色 callback 只读编译当前 Character，Composer 复用相同 model/维护预算。非 observer 装配执行 recover() 冻结未完成草稿/投递，不调用 Sender；默认上下文排除 Fake 回执。真实渠道 Adapter 仍无主动发送接线。完整调用路径、状态和验证见 [outbox.md](outbox.md)。

## Review 修复后的只读启动与会话切换

独立 Web 的 `open_application(observer_only=True)` 使用 `SQLiteStore(read_only=True)`，通过 SQLite `mode=ro` 和 `query_only` 拒绝数据库写入。`initialize()` 此时只验证 schema 17，不执行迁移；`SessionManager` 只加载已有 Agent 和已选 Conversation，不创建身份、不更新 client_state。数据库缺失、版本不符或尚无会话时返回 HistoryError，需先由普通 CLI 初始化。占用锁仍按单实例规则取得，不支持旁路访问正在运行的应用。

可信 CLI 的 `/new`、`/resume` 调用 `ChatService.select_conversation()`，同时更新 SessionManager 和 Agent.context；前台运行中和 observer 模式拒绝切换。Context 的时间回调绑定创建时的 Conversation ID。验证：`tests/test_review_refactoring.py` 的完整 observer 启动覆盖缺失、旧 schema 和已有数据；会话切换沿 `tests/test_config_cli.py` 验证。

# P4.4.1 明确提醒生命周期

`assemble_chat()` 增加绑定当前 Agent 的 `SchedulerRuntime`，保存于 ChatService.scheduler；observer_only 只读装配不恢复或启动。`open_chat()` 在 yield 前 start()，独立有界 timer 不受 foreground/Idle/Attention 门控；ChatService.close() 先取消并等待提醒定时器和在途模拟，再 finally 关闭原维护 Runtime。没有真实渠道 Sender 接线。创建、取消、恢复状态、异常和待执行验证详见 [scheduler.md](scheduler.md)；本轮按用户要求未运行测试。
