# Shared Chat Runtime

## 职责与架构组成

`src/personal_agent/runtime.py` 的 `open_application()` 持有 SQLite 单进程锁、SessionManager、MemoryFS 与 Provider 生命周期。`open_chat()` 管理 SDK 客户端；`assemble_chat()` 复用现有 ContextBuilder、工具注册、Reflection、P1 Organizer、P2 Learning/Curator/Adaptive Retrieval、P3 Character Package/State，返回轻量 `ChatService`。渠道不各自装配学习或角色服务。

## 代码实际运行路径

1. `main.run_cli()` → `open_application()`：初始化 schema，恢复遗留 Run 的审计状态，再恢复已选 Conversation；不会重试工具。
2. 初始化同一 Agent 的 MemoryFS，然后 `open_chat()` → `assemble_chat()` 加载角色、检索与普通工具，绑定现有 P1/P2/P3 服务。
3. CLI `main._chat()` 在本地解析管理命令；普通文本通过 `ChatService.run()` → `foreground()` 取消并等待后台维护，再调用原有 `Agent.run()`。权限仅来自可信 Host 参数。
4. 渠道展示回答后调用 `after_reply()`，沿用 `OrganizationRuntime.schedule()` 的空闲、累计数量和维护预算。它不逐条强制整理 Episode。
5. 退出上下文先关闭后台任务和输入桥，再关闭 SDK、SQLite 与占用锁。

## 异常与边界

Agent 的失败继续返回 `RunResult`；初始化失败沿原 CLI 安全错误路径返回。SDK retries 固定为 0。文件与 SQLite 的提交窗口、Reflection 审批和 P2 回执核对不变。`Agent.run()` 的现有锁串行处理前台 Run；渠道负责限制输入积压和外部回复失败。单个数据库只允许一个本地应用进程。

## 阅读与验证

运行 `uv run personal-agent`。`tests/test_config_cli.py` 验证 CLI、SDK 关闭与会话装配；`tests/test_p2_cli.py` 验证唯一 Learning 入口、共享检索器及只读管理状态。离线测试用 Fake Provider，不连接真实模型。
