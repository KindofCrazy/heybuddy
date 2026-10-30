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

### 可选 LLM 完整性判断

判断器默认使用 `AGENT_QQ_MESSAGE_JUDGE_REASONING_EFFORT=none`，以请求级参数要求关闭推理；可选 `minimal` / `low`，或 `default`（完全不传 reasoning 参数，沿用端点默认值）。这不会改变主对话模型的设置。各模型与兼容端点支持的档位不同，需按服务文档选择；不支持 none 时可以手动改为该模型支持的低档位。不自动探测、不自动换档或重试；Provider 拒绝参数时回退时间聚合。Prompt 同时明确“只判断表达是否结束，不分析或求解用户的问题”，但 Prompt 不能替代 API 的推理控制。参数映射及离线 wire 测试见 [provider.md](provider.md)。

默认 `AGENT_QQ_MESSAGE_MODE=time`，保持纯时间聚合、不增加模型请求。显式设置为 `hybrid` 后，`adapter.py::_check_turn()` 在静默 `AGENT_QQ_MESSAGE_JUDGE_IDLE_SECONDS=0.8` 秒时，通过 `completeness.py::judge()` 复用 `service.agent.provider`（同一已配置模型）额外发起完整性判断。每组最多 `AGENT_QQ_MESSAGE_JUDGE_MAX_CALLS=2` 次，累计判断耗时预算 `AGENT_QQ_MESSAGE_JUDGE_BUDGET_SECONDS=1.0` 秒；短静默必须小于普通 idle，次数限 1～3，预算必须大于零且不超过 5 秒。这里只限制等待耗时和请求次数，不宣称限制服务商最终计费。

请求仅包含固定 system 和当前组文本的 user JSON，无历史、Memory、人格、图片 URL 或工具。输入 JSON 至多 8192 UTF-8 字节，输出 tokens 上限 64（包含模型推理消耗）；仅接受 `ready` / `wait` / `uncertain` 单词。工具调用、非法或过长输出、Provider 失败和超时均按 uncertain 回退，不执行返回的工具、不打印正文或异常体、不自动重试。图片消息（包括混合图文）、命令、已形成硬边界或已到期输入跳过判断。小输出预算可能不足以支持某些推理模型，届时安全回退；离线测试不证明真实完整性识别质量。

`ready` 绑定整组 sequence，仅在 `claim_turn()` 事务重算结果仍精确匹配时允许提前领取。`wait` 将当前末条之后的 idle 边界延长到首条 max_wait；后续消息在该边界内可加入，加入后按新末条重算 idle，仍受硬容量、命令和最大等待约束。uncertain 不延长当前末条等待；先前有效 wait 形成的连接仍保留。每个快照只判断一次，次数或耗时用尽后不再判断，沿用已有时间边界。

`accept()` 仅在真正接收新消息后增加版本；判断 await 期间版本变化，结果即丢弃。判断耗时仍计入当次预算，原有有效 wait 不因旧结果被替换。下一轮消费者重新读取持久 Inbox；判断期间和 timeout/cancel 时都不领取消息，正常关闭/重启无需恢复模型任务。提示和预算是瞬时状态，不持久化，重启重新采用时间策略并重新计数；不能把跨反复重启的调用次数视为持久预算。判断失败不会创建 Agent Run；正式回答才走原 Run/工具/学习路径。费用 usage 不计入正式 Run 的 Journal，当前没有新增判断请求的持久审计或计费汇总。

专项测试 `tests/test_qq_completeness.py` 覆盖枚举与工具拒绝、提前提交、wait 跨 idle、max_wait、快照过期、数据库领取复核、调用上限、超时/关闭恢复和时间回退；仅用 Fake Provider，不产生真实费用。启用后在 QQ 对“我最近在考虑”与明确完整问题分别体验等待和提前回复；准确性与延迟需用户另行真实验证。可以随时恢复 `time` 模式。

离线运行 `uv run pytest tests/test_qq_turns.py tests/test_qq_adapter.py tests/test_qq_images.py tests/test_qq_integration.py tests/test_qq_transport.py`。`test_qq_turns.py` 用固定接收时间和 claim 的 now 参数验证 idle 重置、max_wait、恰好截止、积压边界、条数/字节/图片拆分和回执原子状态；异步测试验证共享 Run、一次回复、after_reply、取消恢复、发送期间到达、失败和 uncertain。原 QQ/CLI 回归继续使用 Fake Provider / MockTransport，网络禁用保护保持不变。

手工验证使用现有已授权 QQ 配置运行 `uv run personal-agent qq`（会使用正常模型调用，请自行决定何时运行）：

1. 每条间隔小于 2.5 秒，依次发送“我最近在考虑”“给 heybuddy 加入主动对话功能”“你觉得应该怎么设计？”。最后一条后静默约 2.5 秒，应看到一次问题回复；Observatory 应只有一个新 Run。Humanizer 可能拆成多个气泡。
2. 两条消息间隔超过 2.5 秒，应产生两个独立输入；持续每秒发送一条，应在首条后约 8 秒提交首组，余下形成新组。网络或前序 Run 忙碌会增加实际等待。
3. 普通文本中间发送 `/new`，应独立返回 CLI 提示，且不切换会话；命令前后的普通文本不合并。
4. 快速发送图片和说明，应在同一输入中理解图片；超过图片总数上限时拆为后续组，不突破单 Run 的原图片限制。
5. 开始聚合后立即 Ctrl+C，再启动，应恢复未执行输入；回答生成或发送期间退出后再启动，不应自动重跑该组。正在生成回复时再发两条，应作为后续输入。

共享渠道协议及整体生命周期见 [qq.md](qq.md)，SQLite 事务与源数据边界见 [storage.md](storage.md)。

## NapCat 主号输入状态

仅 NapCat 的 `runner.receive()` 将已校验 input_status 交给 `QQAdapter.note_typing()`。内存 `extend_after` 以 receipt sequence 标记该条之后的延长截止，传入 `QQInbox.preview_turn()/claim_turn()` 和 `plan_turn()`，从而允许后续消息跨过原静默窗口合并；最大等待与所有硬边界保持有效。消费者在有效提示期间不采用 hybrid 提前 ready，状态变化会使正在等待的模型判断失效；结束提示缩短扩展到通常静默间隔。提示不创建消息/Run、不落库，不取消已领取的 Run。验证见 `tests/test_napcat_input_status.py`，配置见 [NapCat](napcat.md)。
