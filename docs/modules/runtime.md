# Shared Chat Runtime

## 职责与架构组成

`src/personal_agent/runtime.py` 的 `open_application()` 持有 SQLite 单进程锁、SessionManager、MemoryFS 与 Provider 生命周期。`open_chat()` 管理 SDK 客户端；`assemble_chat()` 复用现有 ContextBuilder、工具注册、Reflection、P1 Organizer、P2 Learning/Curator/Adaptive Retrieval、P3 Character Package/State，返回轻量 `ChatService`。渠道不各自装配学习或角色服务。

## 代码实际运行路径

1. `main.run_cli()` → `open_application()`：初始化 schema，恢复遗留 Run 的审计状态，再恢复已选 Conversation；不会重试工具。
2. 初始化同一 Agent 的 MemoryFS，然后 `open_chat()` → `assemble_chat()` 加载角色、检索与普通工具，绑定现有 P1/P2/P3 服务。
3. CLI `main._chat()` 在本地解析管理命令；普通文本通过 `ChatService.run()` → `foreground()` 取消并等待后台维护，再调用原有 `Agent.run()`。权限仅来自可信 Host 参数。
4. 渠道展示回答后调用 `after_reply()`，沿用 `OrganizationRuntime.schedule()` 的空闲、累计数量和维护预算。它不逐条强制整理 Episode。
5. 退出上下文先关闭后台任务和输入桥，再关闭 SDK、SQLite 与占用锁。

QQ 入口 `channels.qq.runner.run_qq()` 也调用 `open_application()`，然后把同一 `ChatService` 交给 `QQAdapter`，只传净化后的文本和空权限。它不调用 `main._chat()` 的本地管理命令解析器。P3 的 `CharacterContextCompiler`、`CharacterStateService` 及历史角色标签都沿原路径恢复；P2 的 Learner、Curator、回执核对与 Adaptive Retrieval 也使用同一装配。`tests/test_qq_integration.py` 验证 CLI→退出→QQ 的历史、Memory、已产生的 Character State，以及 QQ 抢占真实后台 Learning。

## 异常与边界

Agent 的失败继续返回 `RunResult`；初始化失败沿原 CLI 安全错误路径返回。SDK retries 固定为 0。文件与 SQLite 的提交窗口、Reflection 审批和 P2 回执核对不变。`Agent.run()` 的现有锁串行处理前台 Run；渠道负责限制输入积压和外部回复失败。单个数据库只允许一个本地应用进程。

## 阅读与验证

运行 `uv run personal-agent`。`tests/test_config_cli.py` 验证 CLI、SDK 关闭与会话装配；`tests/test_p2_cli.py` 验证唯一 Learning 入口、共享检索器及只读管理状态。离线测试用 Fake Provider，不连接真实模型。
# Observatory 接线

`ChatService.run` 在 try/finally 设置 `foreground_active`，取消或失败也会清除。`qq_status` 是 QQ runner 绑定的无参数读取 callback，仅返回 Gateway/consumer 生命周期枚举。HTTP 不调用这两个执行入口；它只读取标记与持久元数据。

`dashboard/app.py:create_app` 接受同一个 `ChatService`，只保留读取投影。QQ 的 `run_qq(..., dashboard=True)` 在 `open_application()` 内通过 `AsyncExitStack` 启动 HTTP，复用同一 SQLite 连接、Agent Scope 与 MemoryFS。独立 `web` 同样调用 `open_application()`，不安排后台维护；已有进程持有锁时拒绝启动。详情见 [dashboard.md](dashboard.md)，验证见 `tests/test_dashboard.py`。

