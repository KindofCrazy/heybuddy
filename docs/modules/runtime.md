# Runtime：共享装配与前后台交接

## 职责与架构组成

`src/personal_agent/runtime.py` 是具体依赖的装配点。open_application 管 SQLiteStore、SessionManager、MemoryFS；open_chat 管 SDK/Provider 与服务启动关闭；assemble_chat 创建 Agent、Registry、检索、角色、Reflection、Learning/Curator、Organization、Proactive、Scheduler 和可选 HarnessRuntime。ChatService 提供统一 run/after_reply/select_conversation，不包含渠道协议。

## 代码实际运行路径

1. open_application 打开 Store，取得占用锁，initialize 顺序迁移到 schema 23；非只读模式 recover_incomplete_runs 只更新中断审计状态。
2. SessionManager 恢复当前会话，创建 Agent 级 MemoryFS，非只读模式 initialize；sessions.memory 交给 Context 编译。
3. open_chat 创建 AsyncOpenAI，max_retries=0，通过 OpenAIProvider 和绑定 Agent 的 SQLiteImages 进入 assemble_chat。
4. assemble_chat 加载角色、建立 Outbox 接续读取，注册内建/Memory/Recall 工具。生产不注册 add_numbers；基础检索使用 archival_memory_search 与独立 Recall，Harness 开启后还有工作、规划等工具，不能把旧的“四个工具”当完整生产列表。
5. 创建维护服务和独立 Scheduler，再按设置创建 HarnessRuntime；此时尚未连接 MCP。persona.json 角色采用稳定编译路径，旧 Markdown 角色可装配 CharacterStateService。
6. open_chat 依次启动 Harness/MCP、Scheduler；Shadow 启用时安排已有维护通道。随后 yield ChatService。
7. ChatService.run 拒绝观察模式，设置 foreground_active、note_input，await foreground 抢占维护，再进入 Harness.run 或 Agent.run。Harness 管理命令结果可直接包装为 rounds=0 文本。finally 清除前台标记。
8. 入口展示回答后调用 after_reply，schedule 当前会话；completed 才设置 after_answer。新输入取消维护并等待清理，不复制另一套 Agent Loop。
9. ChatService.close 关闭 Harness 后台/MCP、Scheduler，finally 关闭 Organization；open_chat 再关闭 SDK，open_application 关闭 Store 和锁。

会话切换只在两次前台之间由可信 select_conversation 执行，更新 SessionManager 与 Agent.context，再 bind_context 安装新会话贡献器。图片只传引用，由 Provider 读取绑定存储，不在入口创建独立视觉人格。

## 异常与边界

observer_only 使用 SQLite 只读连接，initialize 只验证当前 schema，不迁移；不创建会话、Memory、不恢复执行或激活维护，Harness 不连接 MCP。ChatService.run 拒绝执行，after_reply 不调度；缺数据库/会话或版本不匹配需先由可信普通入口初始化。仍遵守数据库单实例锁。

正常 Run 错误返回 RunResult；初始化失败向外传播到入口安全处理。工具副作用与 SQLite 不共同事务。MCP 单 Server unavailable 不阻断普通聊天。Scheduler 可接明确提醒 Sender，Proactive 仍只 Shadow，二者不能混淆。详细主循环见[消息主线](../architecture.md)。

## 阅读与验证

使用 Fake Provider 调用 assemble_chat，在隔离数据库建立 SessionManager/MemoryFS，再调用 service.run；测试后 await service.close。`uv run pytest tests/test_config_cli.py tests/test_qq_integration.py tests/test_dashboard_lifecycle.py tests/test_harness.py` 验证共享装配、关闭、只读与来源边界。真实 `uv run personal-agent` 会启用配置的 Provider，不用它作为离线验证。

## 未来改进

显式声明装配依赖和关闭顺序，减少扩展遗漏清理。为 observer-only 与执行模式列出能力差异，并把来源验证与权限传递保持在可信边界。
