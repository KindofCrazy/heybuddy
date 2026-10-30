# NapCat 小号一对一聊天

此入口让已登录 NapCat 的专用 QQ 小号通过 HeyBuddy 与你的主号私聊。复用当前 Agent、选中的 Conversation、Luna、Markdown 记忆、检索和既有后台维护。只接受配置主号的好友私聊，回复为文本；支持图片及图文输入，动图只看首帧。Proactive 仍为 Shadow，没有主动消息、群聊、好友申请处理或任意 QQ 操作工具。

## 准备 NapCat

1. 从 [NapCat 官方项目](https://github.com/NapNeko/NapCatQQ) 安装适合系统的版本，按[安装说明](https://napneko.github.io/guide/start-install)启动并登录专用小号。主号与小号需要先互加好友。
2. 在 NapCat WebUI 的网络配置中新建 **WebSocket 服务端（正向 WS）**，启用，监听 `127.0.0.1`，端口 `3001`，消息格式设为 `array`，填写自选随机 Token。关闭自身消息上报和原始调试输出。这里的端口是 OneBot 端口，不是 WebUI 的管理端口。
3. 同机运行 HeyBuddy。WebUI 也应绑定 loopback；本入口只支持 `ws://127.0.0.1:<port>` 或 `ws://[::1]:<port>`，不接受公网、域名、URL 内凭据或查询参数。Docker 部署需要将 OneBot 端口发布到宿主机 `127.0.0.1`；容器内的监听地址另按部署方式配置。

网络选项以 [NapCat 配置文档](https://napneko.github.io/config/basic)为准；这是非官方客户端接入，存在掉线、账号风控及版本兼容成本，详见 [NapCat 安全说明](https://napneko.github.io/other/security)。安装、扫码和真实账号测试由用户在本机完成，自动测试不登录 QQ。

## 配置 HeyBuddy

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

NapCat 复用 `AGENT_QQ_*` 的队列、聚合、图片、拆条、超时、发送间隔和重连设置。默认静默 2.5 秒、首条起最多 8 秒聚合；普通回答最多四段，每段最多 1500 UTF-8 字节。默认 240 秒是本地待处理/回复新鲜度上限，不代表 NapCat 有官方机器人的被动回复窗口；超时不会改成主动消息或自动重跑。默认 `time` 聚合不增加判断请求，若已有 `AGENT_QQ_MESSAGE_MODE=hybrid` 则会沿用有界判断请求。后台 Learning 等仍按原配置运行并可能使用模型额度。

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
