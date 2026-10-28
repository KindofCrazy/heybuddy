# QQ 官方单人 C2C 渠道

## 职责与架构组成

`src/personal_agent/channels/qq/config.py` 定义可选宿主配置和不含原始错误体的 `QQError`。`api.py::QQAPI` 仅连接固定官方 HTTPS 域名，缓存短期 AccessToken，发现 Gateway，发送绑定原消息的纯文本被动回复。`gateway.py::QQGateway` 实现单分片 Gateway，不包含 Agent Loop，也不读取角色或学习状态。

## 代码实际运行路径

1. Host 验证 `QQSettings.require_qq()`，仅 QQ 入口要求 enabled、AppID、Secret 和单一 OpenID。CLI 不要求 QQ 凭证。
2. `QQGateway.run()` → `QQAPI.gateway()` → `headers()` / `token()`：POST `https://api.bot.qq.com/app/getAppAccessToken`，使用 AppID/clientSecret；随后 GET `/gateway`，校验响应 URL 的 WSS scheme、官方主机及端口。
3. `serve()` 等待 Hello，使用平台的 `heartbeat_interval`，发送 Identify（`GROUP_AND_C2C_EVENT = 1<<25`、`shard=[0,1]`）或已有 session 的 Resume。READY/RESUMED 后分发事件。
4. 宿主接收回调返回后才推进内存 sequence。接收回调必须完成持久化或明确忽略，不能等待模型/回复。周期心跳携带最新已接受 sequence；缺少 ACK 则断开并有限重连。
5. `QQAPI.send_text()` 固定 POST `/v2/users/{user_openid}/messages`，只发送 `msg_type=0`、content、原 `msg_id`、递增 `msg_seq`。不发送主动消息，不自动重试 POST。

## 异常与边界

HTTP 响应最大 65536 字节。Token 用 `SecretStr`，wire logger 禁用；只记录固定安全状态和重连次数。TLS 使用库的证书/主机校验，不禁用证书验证；不从环境隐式取得代理。401/403、鉴权/intent/shard 权限拒绝和封禁立即停止。暂时断线使用最多八次、5→60 秒的指数退避；即使中途 READY 也不清零预算，避免快速失败无限循环。无效 session/seq 清理 Resume 状态后重新 Identify；进程重启不恢复 Gateway session。

SDK 审查：腾讯 [botpy gateway.py](https://github.com/tencent-connect/botpy/blob/master/botpy/gateway.py) 当前使用固定 30 秒心跳、未检测 ACK、op7/9 不主动退出，`http.py` 在 ConnectionResetError 后重试请求；其 raw DEBUG/异常输出不满足本项目隐私要求。因此参考官方协议实现小型单分片适配，不引入 SDK 的 APScheduler 或 OpenClaw Runtime。WebSocket 基础协议交给 `websockets` 库。

## 阅读与验证

`uv run pytest tests/test_qq_transport.py` 使用 Fake Socket、MockTransport，验证 token 缓存、仅被动发送、发送超时、Gateway URL、Identify/Resume、ACK、去重游标交接、鉴权拒绝和有界退避。不需要真实 QQ/LLM 凭证。

## 协议核对（2026-10-28）

- [官方 C2C 事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/c2c_message_create.html)：`C2C_MESSAGE_CREATE`，`author.user_openid` 是平台身份，不能用普通 QQ 号码替代；官方示例是 opaque 字符串，程序不猜测长度等于 32。当前文档还描述 `message_scene.ext` 的 msg_idx。
- [官方 WebSocket](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/websocket.html) 与 [Webhook](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html) 都有文档。用户已在目标机器人后台确认「当前以 WebSocket 方式接收事件推送」，因此仅实现 WebSocket；不推断所有新建机器人都有该权限。参考腾讯 [openclaw-qqbot](https://github.com/tencent-connect/openclaw-qqbot)，不引入其 Runtime。
- [当前鉴权](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/api-use.html) 使用 AccessToken，废弃旧 Token；[2026-08-10 变更](https://bot.q.qq.com/wiki/develop/api-v2/changelog.html) 统一生产 API 到 api.bot.qq.com。沙箱保留官方 SDK 的 sandbox.api.sgroup.qq.com，需平台确认权限，禁止自动跨环境回退。
- [消息概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html) 与 [发送单聊消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html) 顶部写 60 分钟/4 次，但后者 msg_id 字段仍写 5 分钟。当前本地默认 240 秒，宿主最多配置 300 秒，最多 4 次；过期不转主动消息。相同 msg_id+msg_seq 重发可能被平台拒绝，也不能据此承诺 Exactly-once。
- 发送接口给出总消息 100 QPS；主动消息的 Bot/关系配额不作为本项目被动发送配额。适配器仍进行保守串行节流，平台 429 不盲目重发。当前页面未给出一致的纯文本硬长度计数规则，采用较保守的 1500 UTF-8 字节本地上限，平台仍可按内容、权限或长度拒绝，不能声称等于官方字符上限。
- 权限、测试账号/沙箱、IP 白名单由 [QQ 开放平台](https://q.qq.com/) 按 AppID 配置。凭据本身不证明 C2C、Gateway 或出口 IP 已被授权。
