# NapCat 本机一对一渠道

## 职责与架构组成

`src/personal_agent/channels/napcat/config.py::NapCatSettings` 保存 loopback WS、Token 与大小号绑定；`events.py::parse_message()` 将 OneBot 数组消息投影为现有 `QQText` 收件结构，不构造虚假官方 Gateway 事件。`transport.py::NapCatTransport` 负责身份核对、收发复用、echo 关联和有限重连。`runner.py::run_napcat()` 负责真实入口的生命周期，不包含另一条 Agent Loop。

复用 `channels/qq/adapter.py::QQAdapter`、`humanize.py::Humanizer`、`images.py::QQImages`、`storage/qq.py::QQInbox`。`QQAdapter.accept_message()` 接受已校验投影；官方入口的 `accept()` 仍使用官方 `parse_text()`。新增显式 `scope_id` 仅改变 Host 绑定校验，模型不能传入。NapCat 的收件 app_id 使用 `napcat:<self_id>:<peer_id>` 命名空间，与官方 AppID 及其他大小号组合隔离。复用 schema 16 的既有 `qq_receipts`，没有新迁移或第二份聊天历史。

## 代码实际运行路径

1. `main.main()` 选择 `napcat`，`config.load_settings()` 读取当前目录 `.env` 和环境变量。`run_napcat()` 先调用 `require_napcat()`，缺少启用、Token 或两个不同 QQ 号码时先于 Provider/数据库打开失败。库级角色仍默认 none，示例显式选择 luna。
2. `NapCatSettings.delivery_settings()` 从共享 `AGENT_QQ_*` 策略构建有界收件/发送设置，覆盖账号绑定，不使用官方凭据。`runtime.open_application()` 打开原数据库锁、SessionManager、MemoryFS、角色和唯一 ChatService。可选 Observatory 进入同一个 `AsyncExitStack`。
3. `NapCatTransport.run()` 通过 `LocalConnect` 连接固定本机正向 WS，拒绝 HTTP 重定向，使用 Authorization Bearer Header，禁用环境代理及 wire logger，限制 frame 为 262144 字节、接收队列为 4，使用协议 Ping/Pong 检测断线。
4. `serve()` 在连接超时内发送 `get_login_info`，校验 echo、status、retcode 与返回的 user_id。校验前最多暂存配置 queue_size 个事件；不接收进 Journal，也不启动消费者。校验失败或身份不符不会发送消息。成功后 `ready()` 启动一次 `QQAdapter.start()`，再依到达顺序提交暂存和后续事件。
5. `parse_message()` 只接受 post_type=message、private/friend、正确 self_id/user_id/sender.user_id。忽略群聊、临时会话、自身 message_sent、其他联系人和管理通知。验证整数时间、未来偏移、数值消息 ID、有界段数和 UTF-8 大小。只投影显式 text 和 image.url；不读取 raw_message、昵称、图片本地路径或展开 reply。face 仅作通用文本占位，其他附件类型不进入模型。
6. `QQAdapter.accept_message()` → `QQInbox.receive()` 短事务去重并暂存 → `ChatService.note_input()` → 唤醒单消费者。回调不等待模型或图片下载，所以 socket reader 可同时处理发送回执。正常消费、聚合、前台抢占、图片校验及 Journal 路径沿用 [QQ](qq.md) 和 [消息聚合](qq-turns.md)。普通对话传入空 permissions，管理命令提示本地 CLI。P2、角色与 Proactive Shadow 沿用共享运行时。
7. 回答调用 `NapCatTransport.send_text()`，再次校验目标主号、来源消息 ID、段数和字节上限，只发送固定 `send_private_msg`。文本以数组 text 段编码，模型生成的 CQ 字符串保持字面文本。每次 UUID echo 对应一个 future；`serve()` 收到相同 echo 后交回 status/retcode/data。只有有效成功响应且包含数值 message_id 才算确认。
8. 外部发送前保存 sending，确认后立即更新段数，完成后 replied。异常/超时、回执缺失或断线保留 uncertain，不重发；断线令所有待确认 future 失败。消费者为空时走原 `after_reply()`，不会让 Shadow 建议发送消息。
9. Runner 先监督身份核对任务与停止信号，消费者启动后监督 receiving/worker/stop。任一失败停止其他任务。Ctrl+C/SIGTERM 先禁止新收件，再取消连接和消费者，回收 HTTP 与原应用。重启只恢复未领取的 received；processing/processed/sending 保守 uncertain。关闭等待有界，但同步 SQLite 和 MemoryFS 操作没有硬实时取消保证。

## 异常与边界

`QQError` 复用安全错误通道，NapCat 传输使用 `napcat_*` 固定错误码；共享消费者的 `qq_*` 表示共同收件/发送处理错误。主入口不打印平台响应、Token、消息或异常原文。HTTP 401/403 与账号不符立即停止；其他连接失败复用最多八次、5→60 秒有界退避，成功连接不清零计数。SQLite/消费者失败停止应用，不继续重跑消息。没有平台事件 Resume 或离线补收保证。

默认 240 秒是本地新鲜度限制，最多四段是 Host 限制，不声称属于 OneBot 协议约束。SQLite 与 QQ 无共同事务，confirmed 表示接口确认，不表示对方已读；不能保证 exactly-once。回执墓碑随收件增长。换主号或小号后旧命名空间不自动消费；切换 Conversation 时，当前命名空间的旧 received 沿用既有失败终结规则。

图片继承原 HTTPS CDN、像素/字节/超时校验和规范化，不使用本地文件、base64 或任意下载接口。群聊和好友管理没有入口。远程消息不授予 Memory 权限；手动 Reflection 审批不变。角色和后台学习可能调用已有配置模型，测试禁止真实模型网络；没有新增依赖或通用 Scheduler。

## 阅读与验证

最小真实入口：按 [接入指南](../napcat-setup.md)完成大小号、OneBot 与模型配置，然后运行 `uv run personal-agent napcat --dashboard`。离线验证使用 `uv run pytest tests/test_napcat.py`，无需安装 NapCat 或真实 QQ 凭据。

`tests/test_napcat.py` 验证配置与 CLI 路由、账号及事件投影、身份核对前事件缓冲、echo 关联、CQ 字面发送、目标限制、发送超时/断线/失败/取消、真实 loopback WebSocket 鉴权、有限重连退出、Luna/图片/Journal/权限复用、收件重启去重与 Runner 退出。FakeProvider、MockTransport 与测试 WS 仅使用合成数据；`tests/conftest.py` 的外网禁用保护保持不变。共同消费者的队列、聚合、数据库故障、重启与取消覆盖继续由 `tests/test_qq_adapter.py`、`tests/test_qq_turns.py` 和 `tests/test_qq_images.py` 验证。
