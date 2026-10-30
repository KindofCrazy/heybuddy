# QQ 官方单人 C2C 渠道

## 与本地 CLI 的能力差异

QQ 仍使用相同角色、Core 注入、主题/历史检索、会话压缩、Episode/Thread、P2 Learning/Curator 和角色状态；后台任务是否执行取决于共享配置与调度条件，不因 QQ 而全部关闭。

QQAdapter._process() 对所有斜杠管理命令返回本地 CLI 提示，普通消息明确传入空 permissions。因此不提供 memory_read、memory_edit、memory_update、deferred_create、deferred_edit、deferred_index_repair，也不开放 /reflect 审阅批准、索引修复或会话管理。human.md 与 MEMORY.md 仍自动提供，统一主题检索和 Recall 仍可用；后台 Learning 的写入权限独立，不需要主模型获得 memory.update。

目前没有 QQ 远程管理权限开关。如需开放，应新增受信白名单管理命令解析，逐 Run 授权并保留批准对象绑定，不能把普通消息内容当作权限。当前仍保持原策略。生产入口移除 add_numbers 后，普通 QQ 请求通常提供四个工具，终轮不提供工具。

## 职责与架构组成

`src/personal_agent/channels/qq/config.py` 定义可选宿主配置和不含原始错误体的 `QQError`。`api.py::QQAPI` 仅连接固定官方 HTTPS 域名，缓存短期 AccessToken，发现 Gateway，发送绑定原消息的纯文本被动回复。`gateway.py::QQGateway` 实现单分片 Gateway，不包含 Agent Loop，也不读取角色或学习状态。

`humanize.py::Humanizer` 仅负责发送计划中的自然拆条和轻量停顿，不调用模型、不改人格或记忆、不执行网络发送。`adapter.py::QQAdapter` 继续统一处理节流、回复期限、取消和回执。模型生成的完整回答仍只按原样保存在 Journal，不将气泡拆成多个模型消息。

## 代码实际运行路径

1. Host 验证 `QQSettings.require_qq()`，仅 QQ 入口要求 enabled、AppID、Secret 和单一 OpenID。CLI 不要求 QQ 凭证。
2. `QQGateway.run()` → `QQAPI.gateway()` → `headers()` / `token()`：POST `https://api.bot.qq.com/app/getAppAccessToken`，使用 AppID/clientSecret；随后 GET `/gateway`，校验响应 URL 的 WSS scheme、官方主机及端口。
3. `serve()` 等待 Hello，使用平台的 `heartbeat_interval`，发送 Identify（`GROUP_AND_C2C_EVENT = 1<<25`、`shard=[0,1]`）或已有 session 的 Resume。READY/RESUMED 后分发事件。
4. 宿主接收回调返回后才推进内存 sequence。接收回调必须完成持久化或明确忽略，不能等待模型/回复。周期心跳携带最新已接受 sequence；缺少 ACK 则断开并有限重连。
5. `QQAPI.send_text()` 固定 POST `/v2/users/{user_openid}/messages`，只发送 `msg_type=0`、content、原 `msg_id`、递增 `msg_seq`。不发送主动消息，不自动重试 POST。

## 自然拆条与发送节奏

`QQAdapter._process()` 用 monotonic 记录本次处理耗时，将完整回答和耗时交给 `_reply()`。`Humanizer.split()` 先调用 `events.split_reply()` 取得原有 UTF-8 字节限制计划，再仅用剩余段数按换行及句末标点拆成聊天气泡；短于六字符的片段优先和后一句放在一起，溢出片段按原空白合回末条。因此自然拆条不会挤占原本能发送的正文容量，也不会超过宿主的段数/字节上限。

识别到代码标记、缩进代码、列表、标题、表格、引用、链接或数学标记时，整条回答跳过额外自然拆条。它们仍遵守原平台字节限制，过长时仍可能因硬限制分段或附续问提示；不承诺跨段保留完整代码块。普通文本的段首尾空白会被去除，不改写正文。该规则是保守的语法识别，不是语义分类模型。

`Humanizer.delay()` 对首段按字数估算暂停，默认 25ms/字符，加入 ±20% 波动后再限制为最多 1.5 秒；本次前台处理耗时从首段暂停中扣除，生成较慢时直接发送。后续段以默认 1.2 秒条间暂停加同样波动，并受同一单次上限限制。暂停和 `qq_send_interval_seconds` 取较大值，不叠加等待。每次暂停均使用可取消的 `asyncio.sleep()`，先检查回复窗口；装饰性暂停会耗尽发送预留时间时直接省略，但仍执行必要节流。醒来后再次检查期限，过期停止发送，不转成主动消息。

`AGENT_QQ_HUMANIZE_ENABLED=false` 同时关闭额外拆条和装饰性停顿，恢复原字节限制拆分及固定节流。其余有界配置为 `AGENT_QQ_HUMANIZE_MS_PER_CHAR`（0–150）、`MAX_DELAY_SECONDS`（0–5）、`GAP_SECONDS`（0–2）、`JITTER`（0–0.5），后三者也使用 `AGENT_QQ_HUMANIZE_` 前缀；单次暂停上限设为 0 可只体验拆条。没有深夜减速、模拟睡眠、输入状态 API 或额外后台任务。

发送成功后立即保存 `sent_parts`，下一段等待期间仍可查看确认进度。关闭、发送异常或存储错误保留内存中已确认段数；重启按既有规则将未终结回执归为 uncertain，不恢复剩余气泡。等待期间的新输入按既有队列串行处理，不打断已生成回答；Ctrl+C 可取消等待。外部发送成功但回执落库失败仍存在双系统窗口，不声称已回滚或能够可靠重发。

最小离线例子（无需 QQ 或模型配置）：

```python
from personal_agent.channels.qq.config import QQSettings
from personal_agent.channels.qq.humanize import Humanizer

human = Humanizer(QQSettings(qq_humanize_jitter=0))
print(human.split("今天终于跑通了？\n之前卡住的地方，最后怎么解决的？"))
print(human.delay("今天终于跑通了？", first=True, elapsed=2))  # 0.0
```

`tests/test_qq_humanize.py` 验证拆条、结构化内容、容量保留、关闭开关、延迟/波动上限与环境配置；`tests/test_qq_adapter.py` 用 Fake Provider、可控时钟和发送器验证完整 Journal、零额外模型请求、生成时间抵扣、节流重叠、期限前后检查、等待取消/排队及第二条发送失败。运行 `uv run pytest tests/test_qq_humanize.py tests/test_qq_adapter.py`；测试不依赖真实等待来证明时间策略，也不证明主观自然度已提升。

## 异常与边界

HTTP 响应最大 65536 字节，整个请求还受总超时约束，逐块缓慢返回不能无限延长等待。Token 用 `SecretStr`，wire logger 禁用；只记录固定安全状态和重连次数。TLS 使用库的证书/主机校验，不禁用证书验证；不从环境隐式取得代理。401/403、Token 业务拒绝、鉴权/intent/shard 权限拒绝和封禁立即停止。暂时断线使用最多八次、5→60 秒的指数退避；即使中途 READY 也不清零预算，避免快速失败无限循环。无效 session/seq 清理 Resume 状态后重新 Identify；进程重启不恢复 Gateway session。Adapter 构造时复核数据库实例、Agent/AppID/OpenID 绑定，发送 API 再核对目标白名单、段数和大小；进程内回复期限同时受 monotonic deadline 约束，墙钟回退不会延长已领取消息的窗口。

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

## Adapter 与收件回执执行路径

`events.parse_text()` 投影 C2C 非机器人用户的文本与图片附件，保留图文组合并允许纯图片；图片卡片只取受限 preview，不展开引用或其他卡片。字段大小、UTF-8、时区时间戳和未来偏移有界。只使用 author.user_openid；不从昵称/union_openid/内容推断授权。`QQAdapter.accept()` 在白名单检查后调用 `storage.qq.QQInbox.receive()`，事务提交后通知单消费者，随后 Gateway 才推进 sequence。接收路径不下载图片或等待 LLM。图片下载、校验、持久化和模型输入的完整路径见 [图片输入](images.md)。

schema 13 原子追加 `qq_receipts`，沿用同一 SQLiteStore 和数据库锁。回执仅保留平台标识、时间、状态、Conversation/Run 关联和段数；仅 received 阶段暂存输入文本，领取时删除这份队列文本。原始完整历史仍只保存在 messages；不创建 QQ 历史或事实数据库。

`_consume()` 按收件 sequence 领取，再调用共享 `foreground()`。普通文本 → `ChatService.run(..., permissions=frozenset())` → 原 Agent/ContextBuilder/Journal；以 `/` 开头的输入返回固定不支持提示，不调用任何 CLI 管理分支。排队不会取消正常 Run；消费者空闲时才调用 `after_reply()` 复用现有维护策略，不强制逐条切 Episode。

领取前提交 processing，回答后提交 processed，再于每段外部发送前提交 sending。发送成功更新 sent_parts，全部完成才记 replied。失败不会重跑 Agent；发送超时、失联、平台审核等无法确认结果时记 uncertain，不重发剩余段。部分已发送的文字无法回滚。发送节流跨不同输入保持串行；每段检查原始时间及本地接收时间，不能延长被动窗口。

去重键为 `(agent_id, app_id, message_id)`，非空 event_id 另有唯一索引；msg_idx 仅作为 metadata。刻意保守按 message_id 拒绝重复，即使重复投递缺少或改变 msg_idx 也不会重复运行。极少数共享同一 msg_id、不同 msg_idx 的独立输入可能被抑制；本版不支持平台并行/引用消息。去重墓碑不自动删除，因此收件回执随消息数增长；队列待处理数默认 16（另有最多一个已领取），不能无限积压。队列满抛出受控错误，不推进当前事件游标，断线后有限 Resume；不承诺平台会无限保留/补发事件。

重启 `QQInbox.recover()` 将 processing/processed/sending 改为 uncertain；已领取但尚未开始 Run 的崩溃窗口也保守放弃。只有 received 可恢复；配置白名单变化不读取旧用户队列，Conversation 被本地切换时旧 received 标记 failed，不重定向到新会话。退出停止接收并取消消费者，正在执行的 Agent 使用既有 cancelled 审计；received 留待下次安全处理。SQLite 失败停止消费，不推进接收游标。数据库与 QQ API 之间没有共同事务，不提供 Exactly-once。

验证：`tests/test_qq_adapter.py` 覆盖授权/未授权、去重、串行/队列上限、命令隔离、伪造权限、Provider 超时、发送失败/不确定、长文本、过期、退出、重启以及 schema 12→13 回滚。

## 启动与关闭

`main.main()` 默认 `chat` 保持原入口兼容；`uv run personal-agent qq` 选择 `runner.run_qq()`。凭据及 OpenID 检查先于 Runtime/Provider 初始化。`run_qq()` 使用 `runtime.open_application()`，同一个 Agent/Conversation/MemoryFS/Character，不增加渠道专用人格 Prompt。接收任务与消费者被一起监督；任一异常会停止另一方。Ctrl+C 或 SIGTERM 先禁止新收件、取消 Gateway，再保守取消前台消费者，最后退出共享应用上下文；WebSocket 关闭握手最多 3 秒，消费者取消等待默认 10 秒。同步 SQLite/MemoryFS 本地 I/O 不是硬实时可取消操作。

`uv run personal-agent qq --discover-openid` 单独通过 `config.load_qq_settings()` 加载 QQ 配置，不要求 OpenAI Key、模型或 Context Window；不打开数据库/MemoryFS/Character。只在官方 TLS Gateway 收到第一条有效 C2C 文本时输出 author.user_openid，绝不打印正文/原事件，不回复，随后退出。默认最多等待 120 秒（`AGENT_QQ_DISCOVERY_SECONDS`，最多 600），不自动允许该 OpenID，须用户核对测试账号后写入白名单。

关闭 P2 自动学习可以设置 `AGENT_MEMORY_LEARNING_ENABLED=false`；同时设置 `AGENT_MEMORY_AUTOMATION=off`，避免启动兼容的建议式 Reflection。已有 Core/Deferred/Recall 检索继续可用。P1/P3 的空闲维护仍按宿主现有配置与预算执行；角色开关和来源与 CLI 相同，关系语义的 persona.md 可由后台 Learner 按真实证据维护，Character Core 仍只读。
# 同进程 Observatory

`QQGateway.state` 从真实连接过程更新为 not_started/connecting/ready/reconnecting/error/stopped。`run_qq` 将 Gateway 状态与 consumer 是否存活绑定到共享 `ChatService.qq_status`，不返回 Session ID、OpenID 或 token；Web 不增加消息消费或发送任务。

`run_qq(settings, dashboard=True, dashboard_port=8765)` 在原应用作用域内启动 `dashboard_server`，不新增 Agent 或数据库 Owner。`AsyncExitStack` 在 adapter 关闭后回收 HTTP，再退出原 `open_application()`；HTTP 查询失败由 Dashboard 自行转换为安全错误，不影响 QQ 消费。入口为 `personal-agent qq --dashboard`。说明与验证见 [dashboard.md](dashboard.md)、`tests/test_dashboard.py`。

