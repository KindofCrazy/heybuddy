# QQ 官方机器人单人私聊接入

当前只支持一个机器人、一个明确授权的测试用户、C2C 文本/图片输入和本地单进程。QQ 使用现有 Agent、Conversation、MemoryFS 和 P3 Character；不会创建渠道专用记忆或人格，也不会主动联系用户。

## 平台准备

1. 登录 [QQ 开放平台](https://q.qq.com/)，创建机器人，在开发设置取得 AppID / AppSecret。密钥只填入本地被 Git 忽略的 `.env`，不要提交、截图或打印。
2. 确认目标应用的消息接收方式。此版本只实现 WebSocket；本次用户已确认后台显示「当前以 WebSocket 方式接收事件推送」。其他机器人若仅开放 Webhook，不能直接使用本入口，需要另行适配签名回调，不能以未经验证的 HTTP 代替。
3. 在平台开放单聊能力，并订阅 C2C 事件；连接使用 `GROUP_AND_C2C_EVENT (1<<25)`，收到群/管理通知会忽略。未发布的机器人按平台要求添加开发/沙箱测试账号，并在 QQ 中添加机器人或打开它的私聊页面。平台审核、发布和测试名单配置仍由用户操作。
4. 如果平台要求出口 IP 白名单，填写运行电脑/服务器访问公网时的出口 IP（非本地 192.168 地址）；更换网络后需更新。允许访问官方 HTTPS 与 Gateway WSS 的 443 端口。此模式不需要域名、反向代理、HTTPS 公网回调或入站端口。
5. 默认使用生产 API。只有后台明确要求 SDK 沙箱域名时才配置 `AGENT_QQ_SANDBOX=true`；不要因为权限失败自动切换环境。机器人注册类型、权限及沙箱规则以该 AppID 的平台页面为准。

## 本地配置与 OpenID

保留现有 `.env` 的模型、数据库、Memory 和 Character 设置；追加 QQ 配置，所有值从 `.env.example` 对照填写。原 CLI 可以继续使用 `uv run personal-agent` 或 `uv run personal-agent chat`。

```dotenv
AGENT_QQ_ENABLED=true
AGENT_QQ_APP_ID=your_app_id
AGENT_QQ_APP_SECRET=your_app_secret
AGENT_QQ_ALLOWED_OPENIDS=
```

AppID 必须替换为实际数字。尚不知道 OpenID 时先运行：

```powershell
uv run personal-agent qq --discover-openid
```

出现 `qq_gateway_ready` 后，在 QQ 中用你自己的测试账号给机器人发一条纯文本，例如“你好”。程序只显示第一条有效 C2C 文本的 `QQ OpenID > ...`，不会显示正文、调用模型、初始化数据库或回复；默认 120 秒退出。将这个身份与实际发送账号核对后，手动填入 `AGENT_QQ_ALLOWED_OPENIDS`。这不是普通 QQ 号码，不从昵称推断，不支持 `*` 或多用户。不应把未知访客的 OpenID 自动加入白名单。

普通 QQ 聊天仍需原有 `OPENAI_API_KEY`、`OPENAI_MODEL`、`AGENT_CONTEXT_WINDOW` 等模型配置；只有诊断模式不需要这些设置。模型调用可能产生费用，离线测试不产生费用。

首次只体验现有 Memory 检索而关闭自动学习时，配置：

```dotenv
AGENT_MEMORY_LEARNING_ENABLED=false
AGENT_MEMORY_AUTOMATION=off
```

只关闭 P2 Learning 会恢复原兼容 Automation 行为，因此同时设为 off。已有记忆检索不受影响；P1 和 P3 的空闲维护仍遵守各自开关、原有策略与预算。角色继续使用同一个 `AGENT_CHARACTER_ID`、本地包与 Character State；QQ 不改变 persona.md，也不添加另一份角色 Prompt。

## 最小真实 Smoke Test

1. 退出正在使用同一数据库的 CLI；不要同时运行 CLI 与 QQ 两个进程。测试前确认 `.env` 中的数据库、Memory、角色配置仍指向原来的 HeyBuddy。
2. 完成平台权限、测试用户、出口 IP 与 OpenID 白名单配置。
3. 运行 `uv run personal-agent qq`，等待 `qq_gateway_ready`。
4. 在 QQ 中发送“你好”，确认收到无 CLI 前缀的自然语言回答；这是正常模型调用。
5. 再发“继续我们之前的 HeyBuddy 讨论”，观察同一 Conversation 的上下文、已有 Memory 和已启用的 Character；模型没有原始证据时应明确不确定，不能把平台元数据推断成事实。
6. 快速连发两条文本，确认按顺序回复；发送 `/memory repair`，应只收到本地管理提示，不能执行维护。
7. Ctrl+C 退出，再次启动后继续同一 Conversation。不要手动重投不确定的旧消息来试图“恢复工具”；发一条新消息核对情况。

模型结果与工具运行保存在原 messages Journal。发送失败不重跑 Agent；某段已发送、后段失败时，已发段不会撤销。回复始终绑定原 `msg_id`、递增 msg_seq，最多四段；长回答按自然断点控制 UTF-8 字节，超出段数给出简短续问提示。当前文档对时效存在冲突，本地保守使用 240 秒，过期后绝不转换成主动消息。

## 聊天气泡与停顿

QQ 默认开启自然拆条：普通聊天按换行或句末标点分成最多四个气泡，同段短碎片合并，空行分隔的段落即使很短也分别发送，例如“在呀”和空行后的“怎么，睡不着吗”会拆为两条；超过条数上限的部分仍合入末条。代码、列表、表格与链接等结构化回答跳过额外拆条。原来的每段字节上限仍适用，自然拆条不会导致可发送正文变少。

首条按 25ms/字符估算轻量暂停，扣除模型生成等前台处理已花费的时间；后续条间默认约 1.2 秒，并有 ±20% 波动。每次装饰性暂停最多 1.5 秒，与原发送节流取较大值，临近回复期限时省略装饰性暂停。没有额外模型调用；不按深夜时段变慢，也不发送“正在输入”状态。退出时取消等待，已经发送的气泡保留确认段数，不自动补发。

配置可从 `.env.example` 对照追加，重启 QQ 进程后生效：

```dotenv
AGENT_QQ_HUMANIZE_ENABLED=true
AGENT_QQ_HUMANIZE_MS_PER_CHAR=25
AGENT_QQ_HUMANIZE_MAX_DELAY_SECONDS=1.5
AGENT_QQ_HUMANIZE_GAP_SECONDS=1.2
AGENT_QQ_HUMANIZE_JITTER=0.2
```

将 `AGENT_QQ_HUMANIZE_ENABLED=false` 可恢复原字节限制分段及固定节流；将 `AGENT_QQ_HUMANIZE_MAX_DELAY_SECONDS=0` 可保留自然拆条、去掉装饰性停顿。`AGENT_QQ_SEND_INTERVAL_SECONDS` 仍是必须遵守的最小发送间隔。CLI 输出不受这些 QQ 配置影响。

## 安全状态与排错

| 现象/安全代码 | 检查事项 |
| --- | --- |
| `qq_configuration` | enabled、数字 AppID、非空 AppSecret；只检查当前目录 `.env` 或显式环境变量 |
| `qq_openid_required` | 白名单须恰好一个已核对的 user_openid；诊断模式可为空 |
| `qq_token_rejected` / `qq_authorization` | 凭据、平台权限、应用状态、出口 IP；程序不输出响应正文 |
| `qq_gateway_permission` | Gateway/intent 权限、机器人下架/封禁；终止而非无限重连 |
| `qq_discovery_timeout` | 测试账号/好友关系、C2C 权限、是否发送纯文本、是否还有其他进程连接该机器人 |
| `qq_queue_full` / `qq_reconnect_exhausted` | 待处理队列默认 16；有限重连用尽后需用户检查状态再启动，不自动重跑 Run |
| `qq_send_failed` / `qq_send_uncertain` | 平台频率、内容、长度、过期或网络；检查 QQ 是否已收到，绝不按日志重发或重跑 Agent |
| 存储占用 | 关闭另一个操作相同数据库的 CLI/QQ 进程；不删除占用锁绕过保护 |

安全日志仅打印固定状态和数值，不打印 Token、用户正文、后台 Learning 候选或原平台事件。平台/网络出错不表示工具副作用已经撤销。进程退出后未领取的 received 输入可恢复，领取过的 processing/processed/sending 一律 uncertain。长时间离线与 Gateway 有限补发能力意味着不能承诺完全不丢消息或端到端 Exactly-once。

## 配置边界与验证

全部默认值见 `.env.example`；`AGENT_QQ_*` 由 `channels.qq.config.QQSettings` 校验。核心执行路径、协议核对、SDK 取舍和恢复语义见 [QQ 模块文档](modules/qq.md)、[Shared Runtime](modules/runtime.md)。

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build --offline
uv run personal-agent qq --help
```

离线测试保留 `tests/conftest.py` 禁网保护。`test_qq_transport.py` / `test_qq_adapter.py` / `test_qq_integration.py` 使用 Fake Gateway、Fake Provider 与 MockTransport，不会读取真实密钥或连接 QQ。

不支持群聊、语音/视频/文件、流式编辑、主动消息、远程管理、并行用户、跨进程共享数据库、自动发送重试或工具重试。未实现 Webhook，不适用挑战/签名测试。账号权限、内容审核、网络与真实模型行为仍须通过上述流程现场确认。

本轮离线检查与真实联调完成程度见 [交付验证记录](qq-validation.md)。

## 图片与表情包

重启原 QQ 进程后，可以发送图片、图文组合或图片表情包；原有角色、会话与 Memory 继续使用同一管线。支持 JPEG、PNG、GIF、WebP，动图只看首帧。默认每条最多四张，每图下载最多 8 MiB，附件准备总计 15 秒；归一化后最长边 1536、每图最多 256 KiB。文字 emoji 继续按文本处理。

图像模型必须支持 Responses 图片输入；deepseek-flash 的当前官方 API 支持此能力。图片下载或解码失败会提示换图，不会假装已看到内容。官方 CDN 之外的 URL、跳转、其他附件和引用消息不开放。当前实现只输入图片、回复文字，不发送图片。详细安全/存储/首帧限制见[图片输入](modules/images.md)。真实 QQ 图像收发与付费模型识别尚待用户实际验证，离线用例不能证明视觉识别质量。
