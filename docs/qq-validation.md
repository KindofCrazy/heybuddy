# P4-MVP 交付与验证记录

日期：2026-10-28。开始 HEAD：`0dd3f434a2fc3280f9f30ed0a798ca0ae8679ad8`，开始工作区干净、分支 main，上游 origin/main。

## 里程碑

| 阶段 | 提交 | 内容 |
| --- | --- | --- |
| M1 | `4879f30` | Shared Chat Runtime、共享生命周期及 CLI 前台执行 |
| M2 | `61ccd48` | 官方 AccessToken、单分片 WebSocket、心跳/Resume/有界重连 |
| M3 | `9105269` | schema 13 收件回执、持久去重、串行消费、权限和失败恢复 |
| M4 | `6dba693` | QQ 启动/限时 OpenID 诊断、P1/P2/P3 完整接线及跨重启集成测试 |
| M5 | 本记录所在的 P4 最终提交 | 配置示例、接入指南、总超时/目标复核/期限保护及最终验证 |

各阶段提交分别推送 origin/main。工作期间另一项任务加入并提交了 Observatory `07469c6`，本任务保留该工作；M5 只纳入 QQ 文件与相应的文档段落。M5 的完整 SHA 用 `git log` 查看，聊天交付同时报告实际提交号。

## 交付结构

- `runtime.py::open_application/open_chat/assemble_chat/ChatService` 负责原有 Agent、Provider、SQLite、SessionManager、MemoryFS、工具和维护装配。CLI 与 QQ 普通输入都通过 `ChatService.run()` 调用原 `Agent.run()`，使用原 ContextBuilder。
- `channels/qq/config.py`：可选 QQ 配置，默认关闭；仅 QQ 模式验证凭据。`events.py` 投影纯文本，`api.py` 管理官方 HTTPS，`gateway.py` 负责协议，`adapter.py` 负责串行队列与回复，`runner.py` 监督接收/消费及退出。
- `storage/qq.py::QQInbox` 仅保存平台回执与待处理输入；messages 继续是完整对话唯一权威源。领取前提交 processing，发送前提交 sending；跨进程保留去重墓碑，不重跑 uncertain。
- OpenID 白名单恰好一个平台身份，默认拒绝；未经授权的事件不调用 Agent 或写 Run。QQ 管理命令只给固定提示，普通输入显式空 Host 权限。原工具 Registry 再执行权限校验。
- P1 Episode/Thread/Dirty Repair/Time-driven Runtime、P2 Priority/Batch/Curator/Adaptive Retrieval/Reconciliation、P3 Package/Context/State 继续复用同一服务；QQ 不理解其内部数据，不建立第二份人格或学习器。

Transport 选择基于用户在目标机器人后台确认的 WebSocket 接收方式。核查腾讯文档/SDK 后采用轻量单分片 Gateway，理由与来源见 [QQ 模块](modules/qq.md)。不包含 Webhook、OpenClaw Runtime 或非官方客户端协议。

## 实际验证

| 验证 | 结果 |
| --- | --- |
| M1 CLI/P2 装配相关回归 | 48 passed |
| M2 协议/CLI/隐私相关回归 | 75 passed |
| M3 队列/协议/SQLite/角色迁移相关回归 | 53 passed |
| M4 渠道与既有 CLI/隐私相关回归 | 96 passed，后续补充的完整入口监督集成测试也通过 |
| 最终 P4 专用快照 `uv run --offline pytest -q --tb=short --maxfail=3` | 750 passed、2 skipped；保留原网络禁用保护 |
| 共享工作区全量 `uv run pytest -q --tb=short --maxfail=3` | 752 passed、2 skipped；包含同时进行中的 Dashboard 测试，非单独 P4 计数 |
| `uv run ruff check .` / `uv run ruff format --check .` | 通过；P4 隔离快照与包含已提交 Observatory 的构建目录分别检查 |
| `uv build --offline` | 通过，生成 sdist/wheel；原工作区产物检查无 .env/.heybuddy/.venv 私有路径 |
| `uv run personal-agent --help` / `uv run personal-agent qq --help` | 通过，原入口和 QQ 参数可用 |

当前 Windows 执行环境默认 TEMP 目录拒绝访问、沙箱内 asyncio 会挂起，因此成功的测试使用授权的沙箱外执行、显式可写的项目 TEMP/TMP 和 UV_CACHE_DIR；测试仍禁止外网。早期有一次 Git 隐私探测夹具超时，之后完整重跑通过。为保留并行工作，P4 最终快照也在独立 worktree 检查。嵌套忽略目录中的 Hatchling 构建曾误扫描临时文件，最终改在源码树外的隔离目录成功构建；未为绕过环境问题放宽 MemoryFS 校验或禁网规则。

## 真实联调状态

| 步骤 | 状态 |
| --- | --- |
| QQ 凭据本地配置 | 已写入被 Git 忽略、未跟踪的当前目录 `.env`；未读取/输出原配置正文 |
| 真实 AccessToken 鉴权 | 成功 |
| 真实官方 Gateway 连接 | 成功，收到 READY，日志仅含固定 `qq_gateway_ready` 状态 |
| 真实 C2C Event | 未收到；120 秒 OpenID 诊断窗口超时退出 |
| 授权 OpenID 白名单 | 尚待用户通过测试账号发送文本并核对；没有自动放行任何身份 |
| 真实 QQ 消息发送 | 未执行 |
| 真实端到端 Agent 回复 | 未执行；没有调用真实收费模型或评审 |

只有已完成的真实步骤才记成功。测试流程、平台需要手动完成的权限/测试用户/IP 操作以及启动命令见 [接入指南](qq-setup.md)。准备好后运行 `uv run personal-agent qq --discover-openid`，取得并核对白名单，再运行 `uv run personal-agent qq`，用“你好”和“继续我们之前的 HeyBuddy 讨论”验证。

## 已知限制

只支持单个授权用户的文本 C2C，不支持主动消息、群聊、富媒体、流式编辑、远程管理或两个进程共享数据库。队列默认 16，另有最多一个已领取输入；去重回执不自动删除，随消息数增长。按 message_id 保守去重，极少数同 ID 不同 msg_idx 的独立输入可能被抑制；不承诺 Gateway 离线事件无限补发。

SQLite 与 QQ API 无共同事务。已领取但尚未创建 Run 的崩溃窗口也标 uncertain，可能牺牲一条输入以避免重跑副作用。部分发送、发送超时和记账失败不自动重发；用户需要核对 QQ 和原 Run。不是端到端 Exactly-once。

当前官方文档对 C2C 时效同时出现 60 分钟和 5 分钟，因此默认只用 240 秒，最多四段、每段本地 1500 UTF-8 字节。该字节上限不是官方字符硬上限声明，平台仍可能因审核、长度、权限或频率拒绝。同步本地磁盘操作不能提供硬实时取消。
