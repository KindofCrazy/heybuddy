# P4.4.1：明确的一次性提醒

## 职责与架构组成

`scheduler/models.py` 定义 SchedulerPolicy、ReminderRequest、不可变 ReminderGrant 和固定错误码 ReminderError。`scheduler/cli.py::command()` 解析明确命令，`scheduler/chat.py::reply()` 为已通过渠道校验的私聊提供受限入口；`scheduler/natural.py::respond()` 处理当前明确用户请求的有限中文语法和缺项追问，不使用模型、不从普通未来计划自动生成任务。`core/calendar.py` 与 `core/reminder_intent.py` 提供纯时间解析和保守意图门控，详见 [时间语法](calendar.md)。`scheduler/runtime.py::SchedulerRuntime` 拥有一个有界 asyncio 定时任务，独立于 Attention、Idle、角色状态和后台 Learning。`storage/reminders.py::ReminderRepository` 绑定 Agent，保存提醒、一次性投递尝试及审计事件。

schema 18 追加 scheduled_reminders、reminder_attempts、reminder_events；schema 19 追加 source_scope 与聊天来源索引，旧记录标为 local；旧迁移不改。scheduled_reminders 同时是单次提醒记录和持久待投递队列，不借用依赖 Proposal/Decision 的主动 Outbox。提醒是 Host 明确指令，不是长期事实；不写 MemoryFS、Run、Recall 或 Reflection 来源。单个 request_id 在 Agent 内唯一，重复相同请求返回原记录，内容、来源或会话不一致则拒绝；取消和终态都不会因重复请求重新开启。

明确提醒可以使用经身份核对的 NapCat 单主号渠道；只有 NapCat runner 为 Scheduler 绑定实时许可，默认库实例、CLI 与官方 QQ 不绑定。循环提醒、cron、外部事件和后台工具任务不在此阶段。FakeSender 仅用于明确本地模拟；明确提醒不经过 Opportunity Scout、Attention 或 Composer，也不授予 Proactive 真实发送权限。

## 代码实际运行路径

1. `runtime.open_application()` 沿原初始化路径原子迁移到 schema 21；`assemble_chat()` 为当前 Agent 创建 ReminderRepository/SchedulerRuntime，附在 ChatService.scheduler。只读装配传 read_only=True，不恢复、不启动定时器。
2. `runtime.open_chat()` 在 yield 前调用 scheduler.start()。启动一个应用级定时 task，没有每项提醒独立 task；真实用户聊天继续走原 Agent，不需要开启 Organization/P2/Proactive，也不等待空闲 25 分钟。普通 foreground() 不取消明确提醒的元数据检查。
3. `main._chat()` 在 Agent.run() 前分派 `/remind` 到 command()。at 接受带时区 ISO8601，in 接受正整数 s/m/h/d。ReminderRequest 严格校验正文和 aware datetime，Repository 再检查 Agent/Conversation、时间范围、凭证模式及幂等性；然后在同一事务保存 scheduled 与 created 事件。模型、历史、记忆和 HTTP 都没有创建或取消入口。
4. create() 保存记录后唤醒定时器，返回 outcome=recorded，并按当前来源/会话是否绑定可用渠道返回 delivery_available。确认不表示已投递，离线仍不保证通知。所有数据库时间规范化为 UTC，原 due_at 不因重启或冷却改写。会话绑定在创建时固定，之后切换 Conversation 不改投递目标。
5. `_background()` 每轮 clear wake 后执行 tick()。启动恢复每次最多 batch_size 个 sending；标 uncertain 并同时更新尝试与审计，不执行 Sender。恢复未完成前拒绝本地模拟，避免恢复扫描碰到本次新启动的投递。每批让出事件循环，恢复完成后不重复扫描正常进行中的发送。
6. `ReminderRepository.advance()` 每轮最多 batch_size 个记录。scheduled 到期后，当前无渠道则改 blocked/channel_unavailable；已过 expires_at 则改 expired。blocked 不在每轮重复记账，只在超过宽限窗口后终结。下一次等待取最早 scheduled.due_at、blocked.expires_at 与 check_seconds 的较小值；创建/取消信号可提前唤醒。关闭应用后不运行，重启按原时间和宽限窗口检查，不产生集中补发。
7. cancel() 只终结 scheduled/blocked。若已 sending，返回 too_late，不声称撤回；accepted/failed/uncertain/cancelled/expired 返回 terminal。已经过期但尚未被定时器处理的记录直接终结为 expired。取消仍可在 timer disabled 或有固定错误码时使用，但只读服务拒绝全部写入。
8. `/remind simulate <id>` 明确构造本地 FakeSender 与绑定记录原会话的 ReminderGrant。simulate() 复核 enabled、模拟 mode 和两次授权快照；claim() 短事务复核完整行、状态、会话、到期与有效期，并原子保存 sending、递增 revision、插入唯一 reminder_attempts。首次 await Sender 前已提交领取，幂等键为 scheduled:<id>。明确提醒不套用 Attention 的兴趣评分或社交冷却。
9. `_send()` 对 Sender 设置独立超时，吞取消/超时的迟到返回也不视为确认。明确失败保存 failed，取消、未知异常或未确认结果保存 uncertain；成功保存 accepted 和真实 grant.channel，simulation 只表示 Fake 接受，napcat 表示 API 已接受。回执与尝试/事件同事务提交；回执写库失败不能撤销 Sender 的效果，保留 sending，由启动恢复标 uncertain。没有自动或显式重发终态的入口。
10. ChatService.close() 先关闭 Scheduler，再在 finally 关闭原维护 Runtime。Scheduler 撤销实时绑定、停止接收写操作，取消并等待定时器与在途发送，使其尽可能在 SQLite 关闭前记录不确定结果。取消是协作式，不承诺撤回已发送消息或中断不响应取消的外部代码。

## 可信 CLI 与配置

```text
/remind in 10m 喝水
/remind at 2026-10-31T09:00:00+08:00 提交论文
/remind status
/remind list
/remind list <next_cursor>
/remind show <id>
/remind events <id> [after_event_id]
/remind cancel <id>
/remind simulate <id>
```

创建、取消和模拟不会写成用户 Run。CLI list 每页默认 12、最多 20，事件每页最多 20；Agent 隔离在查询和写入时复核。无时区、过去时间、空正文、无效时长和超出未来范围会返回固定安全错误码，不打印 Pydantic 输入或异常体。Observatory 暂无提醒页面，当前通过 CLI/Host API 或受限聊天命令查看。

## QQ/NapCat 聊天接入

1. `QQAdapter.accept()/accept_message()` 沿原渠道身份、私聊白名单和收件去重路径接收输入。`plan_turn()` 将命令作为独立消息边界，`_check_turn()` 跳过完整性模型判断；不会与前后普通文本拼接执行。
2. `_process()` 在 Agent.run 前仅将首个词为 `/remind` 的消息分派到 `scheduler/chat.py::reply()`。仅允许 help、at、in、list、show、cancel；其他管理命令和 simulate/events/status 不开放。含附件的提醒命令直接提示改用纯文本，不下载附件或调用模型。
3. `source_scope()` 从 Inbox 绑定的 Agent、渠道应用与对方身份生成摘要；`request_key()` 再绑定原 message_id。相对 in 使用持久 received_at，重复处理不会把到期时间顺延。shared command() 接受 Host 提供的 request_id、reference_at 与 source_scope，使用原校验与 create() 事务；模型与历史均不能调用这个入口。
4. list 每页五条，只返回当前来源和 Conversation；show/cancel 同时复核二者，不暴露本地 CLI、其他渠道或其他会话记录。旧 schema 18 提醒保持 local，不能通过私聊管理。分页游标限制在 SQLite 正整数范围内。
5. 数据提交后通过原 `_reply()` 发送被动确认，回复包含提醒 ID、时间、真实记录状态和无到期发送保证的提示。执行开始后标记不可重放；被动确认失败不撤销提醒、不重新排期，沿原收件状态处理 uncertain。收到不确定回执时可发 `/remind list` 查看。

QQ/NapCat 可以直接发送上方 in/at 示例，再用 `/remind list` 或 `/remind cancel <id>` 管理。只读服务拒绝聊天提醒入口，普通对话仍使用空 Memory 写权限。

## 自然语言提醒与缺项追问

1. `main._chat()` 在斜杠管理命令之后、Agent.run 之前调用 `natural.respond()`，绑定 local 来源、当前会话、随机输入 ID 与 Scheduler 时钟。`QQAdapter._process()` 在 Agent 前调用相同入口，绑定原 Inbox 来源、message_id 和 received_at；渠道身份校验和去重继续有效。自然语言明确提醒也是 `turns.plan_turn()` 的单独边界，hybrid 完整性判断跳过这类输入。
2. `reminder_action()` 只识别当前明确的提醒创建、查看、取消或放弃；引文、假设、讨论旧话等不获得创建入口。典型输入为“明天上午九点提醒我交材料”“十分钟后提醒我喝水”“查看我的提醒”“取消刚才那个提醒”。未识别的普通聊天继续进入原 Agent，不增加模型请求或工具。
3. `parse_time()` 使用固定 UTC+8 和该条原收件时间，识别今天/明天/后天、周几、数字日期、中文或数字时刻及相对时长。“九点”缺上午/下午语义时先问，过去时间不会自动滚到明天；不支持的复杂语法不声称设置成功，可改用明确 /remind。Host 执行规则也禁止主模型在没有确认结果时声称设置或取消。
4. `_prompt()` 检查具体时刻与事项，缺项仅通过 `storage/reminder_dialogues.py::ReminderDialogues` 保存 Draft，不创建 scheduled_reminders。schema 20 的 reminder_dialogues 每个 Agent/Conversation/source_scope 最多一条，十五分钟过期；重启可以继续，其他来源与会话不可接续。普通未来计划、假设、明显无关回复不能补全草稿。“算了”只放弃未完成的当前草稿。
5. 回复补全后复用原 request_id、已解析的日期和原锚点，调用 `SchedulerRuntime.create()`；成功后清除草稿。两次 SQLite 提交之间若失败，已提交提醒不会回滚；残留草稿继续使用同一幂等键，不盲目再建。QQ 路径在同步处理结束后标记 execution_started，再沿原收件回执发送确认；取消不能中断短本地发布步骤。
6. “取消刚才那个提醒”仅选当前来源/会话最近三十分钟内最新的一条；最新已终结时不跳过它去取消旧提醒。泛指“取消提醒”“不用提醒我了”先展示有限候选并要求明确 ID，不做批量猜测。自然语言列表每页五条、正文节选不超过 160 字；仍可使用显式命令翻页、查看完整内容。

例如依次发送“明天提醒我一下”→“上午九点”→“交材料”，前两步仅追问，最后才保存提醒。上述处理属于 Host 提醒状态，不写用户 Run、Recall 或长期 Memory。它不承诺理解任意中文，也没有新增通用 Agent 工具。

## NapCat 真实提醒投递

1. `channels/napcat/runner.py::ready()` 在 get_login_info 核对小号身份后，按 `AGENT_NAPCAT_REMINDERS_ENABLED`（默认 true）绑定 `NapCatReminderSender` 和只读许可 callback。许可固定目标主号、Inbox 来源与 Adapter 会话；断线时 callback 返回 None，重连重新记录 connected_at。停止时先 unbind_delivery()。
2. Scheduler 的 `_background()` 先完成有界 sending 恢复；恢复结束后每轮在普通无渠道到期标记之前调用 deliver_due()。`ReminderRepository.delivery_due()` 只取当前 Agent/source_scope/Conversation、仍有效且 due_at 不早于本次 connected_at 的有限批次。离线积压和本地 CLI/其他渠道记录不借用真实 Sender，也不会在重启集中补发。
3. deliver_due() 在领取前重新读取相同许可与 Sender 绑定。claim() 短事务复核完整记录、源和会话、状态、到期与有效期；真实渠道要求 chat 来源精确匹配，保存 sending、一次性 attempt 与渠道标记后才 await。取消或许可变化导致领取失败时不发送。
4. `NapCatReminderSender.send()` 仅接收 scheduled 幂等键，对提醒限速，等待后复核仍为同一个已验证 socket。`NapCatTransport.send_reminder()` 再检查提醒开关、固定主号和正文容量，然后复用 `_send_private()`，发送一条“提醒：…”的 text 数组，不依赖旧消息回复窗口，也不执行 CQ 字符串。只开放固定 send_private_msg，没有任意 OneBot API。
5. transport 必须收到匹配 echo、成功 retcode 与数值 message_id，才返回 SendReceipt。schema 21 追加 delivery_channel（以及到期查询索引）；旧 schema-18 mode 的 CHECK 保留，Repository.record() 将新渠道投影为当前 mode，旧 simulation 回执仍正确识别。accepted/napcat 不等于用户已读。OneBot 没有远端幂等保证，Host 依靠先领取和不重试 uncertain 避免重复执行。

明确提醒到期不随机，不受社交兴趣、安静窗口或前台 Idle 门控；关心式跟进仍走独立 Shadow Attention。应用离线、断线或过期期间不能保证通知，需自行选择可靠的关键事项提醒方式。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| AGENT_SCHEDULER_ENABLED | true | 启用本地明确提醒检查；不授予真实发送能力 |
| AGENT_SCHEDULER_CHECK_SECONDS | 30 | 等待检查上限，近到期项目可更早唤醒 |
| AGENT_SCHEDULER_BATCH_SIZE | 16 | 每批恢复和到期处理上限，最多 64 |
| AGENT_SCHEDULER_GRACE_SECONDS | 86400 | 原到期时间之后的有效投递窗口，最多七天 |
| AGENT_SCHEDULER_MAX_FUTURE_SECONDS | 31622400 | 新提醒最远未来范围，默认 366 天 |
| AGENT_SCHEDULER_TIMEOUT_SECONDS | 10 | 单次提醒投递超时 |

## 异常与边界

数据库故障或运行时错误停止定时器，status 暴露固定 error_code；不自动重试 Sender 或后台工具。过期、取消和发送资格在同一短事务内复核，领取失败不会调用 Sender。数据不是针对本机恶意修改的认证；原数据库占用锁、事务、隐私和普通 Markdown 的权威边界保持。没有通过增加预算或后台模型调用来弥补未处理提醒。

保存只代表保存，blocked 代表到期但渠道不可用，accepted/simulation 只代表 Fake 接受，accepted/napcat 只代表渠道 API 接受。用户已允许真实投递验证；这不等于获得真实模型评测或其他主动消息权限。

## 阅读与验证状态

`tests/test_scheduler.py` 覆盖到期/宽限过期、幂等和取消不复活、一次模拟和重启 uncertain、跨 Agent、只读观察器、非法时间及 schema 17→18 失败回滚。`tests/test_reminder_chat.py` 增加聊天创建/取消、零模型调用、消息去重、来源隔离、附件与远程管理拒绝、游标范围、被动确认失败及 schema 18→19 回滚。最小验证命令为 `uv run pytest tests/test_scheduler.py tests/test_reminder_chat.py tests/test_reminder_natural.py tests/test_reminder_delivery.py`；保留离线网络保护。

`tests/test_reminder_natural.py` 覆盖中文时间、缺项和歧义、重启接续、取消、来源与 TTL、引用/否定/普通计划隔离、发布后清理失败及 schema 19→20 原子回滚。`tests/test_reminder_delivery.py` 通过 Fake Sender 和假 WS 回执覆盖真实 mode、私聊白名单、撤销、断线不补发、一次尝试和 schema 20→21 回滚；pytest 不登录真实账号。2026-10-31 用户允许运行验证，真实投递另用隔离数据库与一条固定测试消息，不触发 LLM；实际结果记于交付记录。
