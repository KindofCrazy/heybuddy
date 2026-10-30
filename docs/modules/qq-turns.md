# QQ 连续消息聚合

## 职责与架构组成

`channels/qq/turns.py::QQTurn` 保存本次计划的有序原始回执和截止时间，`plan_turn()` 只选择连续前缀；不创建 Agent Loop、任务平台或新的历史权威源。`storage/qq.py::QQInbox.claim_turn()` 在原 SQLite 事务内读取至多 17 张 received 回执、规划至多 16 条输入并原子领取。等待时只返回剩余时间，不修改回执。没有新增表或字段，schema 仍为 15；旧数据库及旧回执直接适用。

## 配置与提交条件

`.env.example` 提供 `QQ_MESSAGE_IDLE_SECONDS=2.5` 和 `QQ_MESSAGE_MAX_WAIT_SECONDS=8.0`。同时支持项目惯例的 `AGENT_QQ_MESSAGE_IDLE_SECONDS` / `AGENT_QQ_MESSAGE_MAX_WAIT_SECONDS`，带 AGENT_ 的配置优先。两个值必须有限且大于零，idle 最多 30 秒、max_wait 最多 60 秒，max_wait 不得小于 idle。

首条 received_at 开始 max_wait，每条后续消息重置 idle，先到的截止时间提交。历史积压也按原始接收时间分组，不能因为 Run 很慢或重启把相隔很久的消息合并。恰好位于 deadline 的消息属于下一组。命令立即独立提交；遇到后续命令、时间间隔、不同 Conversation、16 条上限、合并输入字节上限或图片数量上限，也立即提交已有前缀，其余消息仍留 received。普通输入的 UTF-8 总量（包含换行和图片位置提示）不得超过 `qq_input_max_bytes`，总图片数不得超过 `qq_image_max_count`。原始单条输入仍先经过 `events.parse_text()` 的校验；空文本且无图片、异常事件和未授权输入保持原忽略策略。

## 代码实际运行路径

1. `runner.run_qq()` 装配原有共享 ChatService / QQInbox / QQAdapter；`QQAdapter.start()` 先 recover，再启动唯一消费者。
2. Gateway 调用 `QQAdapter.accept()`；`parse_text()` 投影白名单 C2C 文本/图片，`QQInbox.receive()` 提交原回执并按 message_id / 非空 event_id 去重。提交与 `wake.set()` 之间无 await；回调返回后 Gateway 才推进游标。重复消息不会延长 idle。
3. `_consume()` 清除通知，发现待处理输入时先 `await service.foreground()` 抢占维护任务，此时消息仍为 received。`claim_turn()` 在短事务内重新读取并规划，不依赖内存队列。尚未到期时消费者在事务外等待 Event 或 timeout，每次醒来都重新核对持久回执。
4. 到期的连续组在同一事务内全部变为 processing，并清除暂存正文/附件 URL，原始回执 ID、事件 ID、sequence、时间戳仍各自保留。`QQTurn.input_row()` 以换行组合文本、按原顺序组合图片；多消息含图片时增加编号提示以对应各条文字。组内最后一条提供回复引用和平台期限。单消费者保证 claim 返回到开始处理之间没有新消息进入该组。
5. `_process()` 沿用图片准备、安全下载、权限和超时规则，只调用一次 `ChatService.run()`。命令仍只回复 CLI 提示，不调用 Agent。Journal 保存合并后的正常用户输入，不增加 Episode/Thread 或会话机制。
6. Run 返回、超时或正常取消时，`QQInbox.link_run()` 将同一 Run ID 关联到所有组员。`mark()` 的 tuple 输入在一个事务内更新整组状态。组不是 Run：命令、图片失败、过期等可能没有 Run。
7. `_reply()` 仅生成一份发送计划，沿用 Humanizer、跨输入节流、递增 msg_seq 和最多四段。所有段均使用最后一条原始 message_id；不会按回执数量重复回复。每段前核对该引用的平台时间、本地接收时间和 monotonic 处理期限，预留 send timeout。排队和聚合时间不延长平台被动回复期限。
8. 队列清空后 `_consume()` 才调用一次 `after_reply(result)`，继续原维护调度；聚合等待期间不启动新的 after_reply。执行或发送期间收到的消息仍写入 Inbox，待当前组结束后另行规划。

## 异常与恢复边界

聚合等待正常退出或崩溃时，received 保留正文及附件，下次启动安全恢复。已领取但尚未进入 `service.run()` 或回复路径的正常取消，使用仍在内存的原回执，经 `QQInbox.release()` 同一事务恢复 received；例如图片下载时退出。进入 Run 或回复路径后，取消整组为 uncertain，不自动重跑或重发。Run 可能已执行工具；取消审计仍由原 Runtime 负责。

硬崩溃无法使用内存中的原输入，沿用保守策略：processing / processed / sending 全部转 uncertain，即使尚未创建 Run 也放弃自动执行。Run 与回执之间仍无跨调用事务；硬崩溃可能发生在 Run 写入后、关联回执前，因此不能保证异常退出时每张回执都有 run_id。该窗口不触发重试。正常结束、超时和取消均尝试关联整组 Run。

发送失败、过期为 failed；发送超时或平台无法确认投递为 uncertain，保留整组相同 sent_parts，不重跑工具、不补发剩余气泡。SQLite 失败停止消费者；数据库和平台 API 没有共同事务，不承诺 Exactly-once。恢复时不重定向旧 Conversation，不读取其他白名单用户的队列。正常关闭取消等待/消费者，关闭后新 accept 返回 qq_stopping。

deadline 使用持久 UTC 接收时间以支持重启，等待使用 asyncio timeout；机器墙钟大幅跳变仍可能影响分组时机。队列满保持原拒收与 Gateway 有限 Resume 语义；不承诺平台无限保留消息。聚合上限拆组不会丢弃已接收消息，但每组仍受原平台窗口和输入限制约束。

## 阅读与验证

离线运行 `uv run pytest tests/test_qq_turns.py tests/test_qq_adapter.py tests/test_qq_images.py tests/test_qq_integration.py tests/test_qq_transport.py`。`test_qq_turns.py` 用固定接收时间和 claim 的 now 参数验证 idle 重置、max_wait、恰好截止、积压边界、条数/字节/图片拆分和回执原子状态；异步测试验证共享 Run、一次回复、after_reply、取消恢复、发送期间到达、失败和 uncertain。原 QQ/CLI 回归继续使用 Fake Provider / MockTransport，网络禁用保护保持不变。

手工验证使用现有已授权 QQ 配置运行 `uv run personal-agent qq`（会使用正常模型调用，请自行决定何时运行）：

1. 每条间隔小于 2.5 秒，依次发送“我最近在考虑”“给 heybuddy 加入主动对话功能”“你觉得应该怎么设计？”。最后一条后静默约 2.5 秒，应看到一次问题回复；Observatory 应只有一个新 Run。Humanizer 可能拆成多个气泡。
2. 两条消息间隔超过 2.5 秒，应产生两个独立输入；持续每秒发送一条，应在首条后约 8 秒提交首组，余下形成新组。网络或前序 Run 忙碌会增加实际等待。
3. 普通文本中间发送 `/new`，应独立返回 CLI 提示，且不切换会话；命令前后的普通文本不合并。
4. 快速发送图片和说明，应在同一输入中理解图片；超过图片总数上限时拆为后续组，不突破单 Run 的原图片限制。
5. 开始聚合后立即 Ctrl+C，再启动，应恢复未执行输入；回答生成或发送期间退出后再启动，不应自动重跑该组。正在生成回复时再发两条，应作为后续输入。

共享渠道协议及整体生命周期见 [qq.md](qq.md)，SQLite 事务与源数据边界见 [storage.md](storage.md)。
