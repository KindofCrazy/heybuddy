# NapCat 小号一对一聊天

此入口让已登录 NapCat 的专用 QQ 小号通过 HeyBuddy 与你的主号私聊。复用当前 Agent、选中的 Conversation、Luna、Markdown 记忆、检索和既有后台维护。只接受配置主号的好友私聊，回复为文本；支持图片及图文输入，动图只看首帧。Proactive 仍为 Shadow，没有主动消息、群聊、好友申请处理或任意 QQ 操作工具。

## 准备 NapCat

1. 从 [NapCat 官方项目](https://github.com/NapNeko/NapCatQQ) 安装适合系统的版本，按[安装说明](https://napneko.github.io/guide/start-install)启动并登录专用小号。主号与小号需要先互加好友。
2. 在 NapCat WebUI 的网络配置中新建 **WebSocket 服务端（正向 WS）**，启用，监听 `127.0.0.1`，端口 `3001`，消息格式设为 `array`，填写自选随机 Token。关闭自身消息上报和原始调试输出。这里的端口是 OneBot 端口，不是 WebUI 的管理端口。
3. 同机运行 HeyBuddy。WebUI 也应绑定 loopback；本入口只支持 `ws://127.0.0.1:<port>` 或 `ws://[::1]:<port>`，不接受公网、域名、URL 内凭据或查询参数。Docker 部署需要将 OneBot 端口发布到宿主机 `127.0.0.1`；容器内的监听地址另按部署方式配置。

网络选项以 [NapCat 配置文档](https://napneko.github.io/config/basic)为准；这是非官方客户端接入，存在掉线、账号风控及版本兼容成本，详见 [NapCat 安全说明](https://napneko.github.io/other/security)。安装、扫码和真实账号测试由用户在本机完成，自动测试不登录 QQ。

## 配置 HeyBuddy

已按本项目默认目录安装 NapCat 并配置完成的 Windows 用户，可以直接双击项目根目录的 `start-heybuddy.cmd`。它调用 `scripts/start-qq.ps1`，使用已安装的 `.venv` 启动 NapCat 和 HeyBuddy，等待渠道就绪后自动打开 Observatory，不需要每次输入 uv 命令。重复点击会根据本地 PID 和可执行文件路径复用已有服务；启动失败会保留窗口显示提示。脚本从原配置加载账号，不内置 QQ 号码或 Token。

登录状态失效时，脚本会打开本机 NapCat 登录页面，最多等待三分钟让用户扫码确认。HeyBuddy 的运行日志保存在已忽略的 `.heybuddy/napcat/heybuddy.stdout.log` 和 `heybuddy.stderr.log`。关闭启动窗口或浏览器后，已启动的两个服务继续在后台运行；此入口不安装 Windows 服务或开机启动项。首次初始化仍需 `uv sync`、安装 NapCat 并完成下列配置。

需要登录 Windows 后自动启动时，可以在当前用户的 Windows「启动」文件夹创建快捷方式，目标为 `powershell.exe`，参数为 `-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "<项目绝对路径>\scripts\start-qq.ps1" -NoBrowser`，工作目录为项目根目录。它复用同一个启动脚本，不额外创建 Agent 或任务循环；`-NoBrowser` 关闭自动打开浏览器，QQ 登录失效时仍需手动双击启动入口进行扫码。用 `Win+R` 打开 `shell:startup`，删除对应的 `HeyBuddy.lnk` 即可取消自启。该设置仅在当前用户登录后运行，不是登录前的系统服务；移动项目目录后需要更新快捷方式。

在项目根目录已忽略的 `.env` 中保留原模型、窗口、数据库、记忆配置，并设置下列字段。尖括号内容需要替换为本机实际值，不能直接照抄。

```dotenv
AGENT_NAPCAT_ENABLED=true
AGENT_NAPCAT_WS_URL=ws://127.0.0.1:3001
AGENT_NAPCAT_TOKEN=<与 NapCat OneBot 网络配置相同的 Token>
AGENT_NAPCAT_SELF_ID=<小号的 QQ 号码>
AGENT_NAPCAT_PEER_ID=<你的主号 QQ 号码>
AGENT_CHARACTER_ID=luna
```

这里使用普通 QQ 号码，不是官方机器人 OpenID。Token 是 OneBot 网络配置的 Token，不是 WebUI 登录 Token。不需要开启 `AGENT_QQ_ENABLED`，也不需要官方 AppID、Secret 或 OpenID。不要把 Token、QQ 登录目录或私人聊天上传到 Git。

退出占用同一数据库的 CLI、官方 QQ 或独立 Web 实例，然后启动：

```powershell
uv run personal-agent napcat --dashboard
```

不需要 Observatory 时省略 `--dashboard`；面板默认地址为 [本机 Observatory](http://127.0.0.1:8765)，可用 `--dashboard-port 8766` 更换。启动后程序调用 `get_login_info` 核对小号，再开始消费收件；身份不符直接停止。

NapCat 的发送和 humanize 使用独立的 `AGENT_NAPCAT_*` 配置，不再受官方机器人的四段、1500 字节和 240 秒约束。自然拆条默认最多 32 段，每段最多 8000 UTF-8 字节；这是防刷屏和内存占用的本地保护上限，不是平台配额。超出保护容量才会附截断提示，完整模型回答仍保存在 Journal。代码、列表等结构化回答不额外按句拆条。普通待处理/回复新鲜度默认为 900 秒。

每条气泡发送前按该条字符数估算间隔：默认每字符 25ms，后续气泡最低 0.4 秒、最多 3 秒，再加入 ±20% 波动后限幅；首条扣除已用生成时间。无波动时，10 字约 0.4 秒、40 字约 1 秒、100 字约 2.5 秒、长消息最多 3 秒。必需节流默认为 0.3 秒，与装饰性等待取较大值；暂停受回复剩余时间约束，不额外调用模型。设置 `AGENT_NAPCAT_HUMANIZE_ENABLED=false` 可关闭额外拆条与装饰性暂停，基础发送节流仍保留。

主号的“对方正在输入”由 OneBot `notice/notify/input_status` 直接接收，不解析 NapCat 日志，也不向主号发送输入提示。只有正确 self_id、主号 user_id、group_id=0 且时间新鲜的事件有效。非空 status_text 作为输入活动提示，最多延长当前待处理消息的聚合到该事件之后 30 秒；连续新提示可续等，但绝不突破首条消息起 `QQ_MESSAGE_MAX_WAIT_SECONDS`（默认 8 秒）的硬上限。空状态返回通常的静默等待；没收到结束事件也会自然过期。没有待处理文字/图片时提示不创建 Run，已经进入模型的 Run 不会因此取消或重跑。状态不写 Journal、Memory 或用户证据，也不会调用 LLM 判断；hybrid 模式不能提前越过有效输入提示。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| AGENT_NAPCAT_REPLY_MAX_PARTS | 32 | 本地发送段数保护，1～64 |
| AGENT_NAPCAT_TEXT_MAX_BYTES | 8000 | 单段 UTF-8 字节上限，128～16384 |
| AGENT_NAPCAT_REPLY_WINDOW_SECONDS | 900 | 本地新鲜度，最多 3600 秒 |
| AGENT_NAPCAT_SEND_INTERVAL_SECONDS | 0.3 | 基础节流 |
| AGENT_NAPCAT_HUMANIZE_ENABLED | true | 自然拆条及装饰性停顿 |
| AGENT_NAPCAT_HUMANIZE_MS_PER_CHAR | 25 | 每条按字符计算停顿 |
| AGENT_NAPCAT_HUMANIZE_GAP_SECONDS | 0.4 | 后续气泡最低停顿 |
| AGENT_NAPCAT_HUMANIZE_MAX_DELAY_SECONDS | 3 | 单次装饰性暂停上限 |
| AGENT_NAPCAT_HUMANIZE_JITTER | 0.2 | 轻量波动 |
| AGENT_NAPCAT_INPUT_TYPING_ENABLED | true | 主号输入状态参与聚合 |
| AGENT_NAPCAT_INPUT_TYPING_TTL_SECONDS | 30 | 每个新状态最多有效 1～30 秒，仍受整轮硬上限约束 |

队列、入站聚合、图片、网络超时和重连继续复用 `AGENT_QQ_*`。默认静默 2.5 秒、首条起最多 8 秒；`time` 模式没有模型判断，`hybrid` 仍沿用既有有界判断。设置在下次启动时加载；后台 Learning 等仍按原配置运行并可能使用模型额度。

## 手工检查

1. 用主号给小号发送“你好”，确认收到 Luna 的文本回复，Observatory 出现同一个 Agent 的 Run。
2. 连续发两条消息，确认聚合后回复；发图片和图文，确认模型能够读取支持的图片。
3. 向小号发送 `/memory edit`，应仅提示本地 CLI 操作。其他 QQ 号私聊、群聊、临时会话及小号自己发送的消息不应调用模型。
4. Ctrl+C 关闭后重新启动，再发一条新消息。已处理过的消息不会重新调用模型。连接中断时，已发送但未确认的段落保留 uncertain，不自动重发；恢复连接也不保证补收断线期间的消息。

## 限制与排错

- `napcat_configuration`：检查启用开关、两个不同的 QQ 号码和非空 Token。
- `napcat_authorization`：OneBot 网络 Token 不匹配或服务拒绝握手。
- `napcat_identity_mismatch`：NapCat 登录账号与 `SELF_ID` 不一致。
- `napcat_reconnect_exhausted`：检查 NapCat 是否启动、WS 服务是否启用及端口；重连预算耗尽后需重新启动入口。
- 图片仅下载已有允许列表中的 QQ HTTPS CDN URL，不读取 NapCat 返回的本地 `file` 路径，不下载任意网址。不支持的附件会提示重发；语音、文件、视频和合并转发暂不进入模型。单独的 QQ 内置表情以 `[QQ 表情]` 表示，不猜测表情含义；引用消息只读取本次显式文字，不展开被引用历史。

本入口不默认导入 NapCat 历史，不提供远程管理命令或直接记忆写权限。普通模型对话仍会产生正常模型费用；本次实现仅做离线测试。内部执行路径与离线验证见 [NapCat 模块](modules/napcat.md)。
