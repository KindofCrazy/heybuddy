# Composer、Outbox 与受控投递

## 职责与架构组成

`proactive/composer.py::Composer` 复用 OrganizerModel，以角色风格和真实来源生成结构化草稿，不提供工具。`proactive/outbound.py::OutboundRuntime` 管生成、最终复核和宿主绑定的投递；`proactive/delivery.py` 定义不可变 DeliveryGrant、SendReceipt、Sender 和离线 FakeSender。NapCat 的具体网络适配在 `channels/napcat/proactive.py::NapCatProactiveSender`，模型和 HTTP 没有绑定或任意发送入口。

`storage/outbox.py::OutboxRepository` 保存草稿、一次性发送尝试和终态。schema 17 建立 proactive_outbox/proactive_attempts；schema 25 原子追加 delivery_channel、source_scope、source_run_id 和待发送查询索引。为保留旧 CHECK 约束，数据库 mode 仍使用 preview/simulation，读取和分页通过 record() 优先暴露 delivery_channel 为真实 mode；不会把模拟历史升级成真实消息。Attempt 列表关联 Outbox 显示渠道和所投递的草稿正文；明确提醒的独立记录与回执见 [看板](dashboard.md) 的 `/api/reminders`。

## 代码实际运行路径

1. `runtime.assemble_chat()` 将 OutboundContext 绑定到每个 Conversation，并在 ProactiveRuntime.outbound 装配 OutboundRuntime。角色 callback 从当前 CharacterContextCompiler 获取表达和包指纹。普通启动 recover()；observer_only 不恢复、不绑定 Sender。
2. `ProactiveRuntime.run()` 优先处理新用户证据和过期事项。符合 Eligibility Gate 后，先运行已就绪的 Outbound 单元，否则请求 Attention。一个周期仍最多运行一个 Proactive 单元；Scout、Attention、Composer 和投递均走原 OrganizationRuntime 串行空闲维护通道。
3. Attention 的 notify 是判断，不是发送回执。下一单元调用 compose()，begin() 验证实际 Decision、Agent、唯一候选及对应修订，重新核对 Proposal、有效期、completed Run 和精确 user 引文，再保存 composing。草稿绑定当前 Conversation、输入水位、收件 epoch 和角色指纹；有效期不超过 Proposal expiry 与一天。
4. Composer 使用空工具生成至多 1200 字符的 JSON text，通常一两句，不催汇报，不虚构用户经历、结果或提醒。移除角色风格尾部精确匹配的主对话规则，保留角色正文。OrganizerModel 继续校验取消、超时、JSON、tokens、字节和凭证模式。语义自然度仍依赖模型。
5. await 后 _valid() 再核对 Gate、输入水位、epoch、Conversation、指纹和 Proposal Revision。prepare() 在短事务 CAS 核对 composing 和来源，再保存 prepared。过期或新输入使草稿取消；生成失败进入 compose_failed，不自动重新生成。
6. `channels/napcat/runner.py::run_napcat()` 在传输层验证小号登录身份后的 ready() 中，按 `AGENT_NAPCAT_PROACTIVE_ENABLED` 绑定 NapCatProactiveSender 和当前来源/主号/Conversation 的 DeliveryGrant。每次连接增加许可 revision，保存 connected_at；关闭时解绑。未绑定、断线、收件待处理、关闭开关或会话变化均不提供真实投递资格。
7. OutboundRuntime.run() 优先取当前 Conversation、created_at 和 notify 决策时间均不早于 connected_at 的 prepared 草稿。旧草稿、旧决策在重连后只保留审计，不集中补发。没有待投递草稿时执行 compose()，不在一次模型生成里连发。
8. _deliver() 两次读取不可变许可，检查 mode、启用状态和许可未变，再做 _valid()。claim() 在同一短事务内核对草稿状态、Proposal Revision、来源、Conversation 和频率。NapCat 还要求所有用户来源 Run 都有绑定 Agent/app_id/主号/Conversation 的真实 QQ 收件回执；CLI、其他账号或混合来源不能投到该主号。默认最近 24 小时最多两次尝试、两次至少六小时，失败和不确定结果也计入额度。
9. claim() 原子写 sending 和唯一 Attempt 后才第一次 await Sender。NapCat 使用 proactive:<outbox_id> 作为本地尝试键；该键不代表服务端幂等支持。Sender 等待有限节流后，再核对 socket 未换、连接 ready 和 delivery_allowed()：检查许可修订、当前输入、事项、角色、会话及来源，之后调用固定 send_private_msg，目标只能是绑定主号。文本使用数组 text 段，CQ 字符串保持字面文本，不加明确提醒前缀。
10. 只有成功响应含有效 message_id 才 finish() 为 accepted，含义是渠道接受，不代表用户已读。明确发送失败为 failed，超时、取消、断线、异常或回执不明为 uncertain。领取失败保证没有外部发送；发送后写库失败保留 sending，不能声称撤销。终态没有投递重试接口；自动投递被权限、频率或来源拒绝时撤下该草稿，不留作稍后补发。
11. recover() 将 sending 标 uncertain、composing 标 compose_failed、prepared 标 cancelled；accepted/failed/uncertain 不重做。`/proactive retry-compose <id>` 只显式重新生成仍有效的 compose_failed，重新绑定当前输入与角色，不延长有效期。

## 本地模拟与对话接续

`/proactive simulate <outbox_id>` 仍是可信 CLI 的显式本地模拟，使用 FakeSender 和 simulation 许可；simulate() 拒绝真实 Sender 或真实许可。默认不自动模拟。模拟与真实投递共享 _deliver() 的领取、取消和失败处理，但渠道与回执身份分别保存。

`OutboundContext.context()` 只读当前 Agent/Conversation 最近至多三条 accepted 消息。真实消息还匹配 CURRENT_SCOPE.source；Thread 路由启用时，要求草稿领取时固定的 source_run_id 属于 CURRENT_THREAD，后续候选更新不会改写已发送消息的来源。NapCat runner 为关闭 Harness 的兼容入口绑定固定来源。其他来源或 Thread、prepared/failed/uncertain 都不可见。只有离线 Harness 显式 include_simulations=True 才能读取模拟消息；每条保留 mode，外层 simulation 表示是否全部为模拟。

接续内容以 kind=proactive_messages、origin=proactive、trust=assistant_generated_only、user_evidence=false 的低信任 JSON 注入，计入原上下文预算，不进入 pending、Journal、Recall、Episode 原始来源或 Scout 用户证据。P2/Reflection 仍只读取原有真实用户来源；主动内容不能充当用户偏好、关系或完成事实。用户实际回复继续走原 Journal。

## 配置、异常与边界

`AGENT_PROACTIVE_ENABLED=false` 关闭整个流水线，`AGENT_PROACTIVE_COMPOSE_ENABLED=false` 关闭生成和真实投递；NapCat 的 `AGENT_NAPCAT_PROACTIVE_ENABLED` 默认 true，设为 false 则保留 Shadow 分析和草稿。其他渠道不绑定真实 Sender，官方 QQ 仍仅被动回复。`ProactiveRuntime.status()` 显示实际 mode/outbound_enabled，Observatory 展示渠道和回执，所有 HTTP 路由继续只读。

原 23:00–08:00 安静时段、维护预算、Idle 和新输入抢占保持。Scout 命中“进展已聊过”会原子撤下未发送草稿；普通 Thread 元数据补链不递增事项语义修订。网络提交开始后，后来的撤销不能撤回已经产生的效果。应用离线不保证通知，重连不补发。没有新的调度平台、任意 OneBot 接口或后台工具任务。

日志只保存安全错误码，不输出草稿、引文、凭据或 SDK 异常体。自动测试全部使用 Fake Provider 和合成 Socket，不登录 QQ、不调用付费模型。

## 阅读与验证

生产最小入口为 `uv run personal-agent napcat --dashboard`；按 [NapCat 接入指南](../napcat-setup.md)完成本机绑定，启动后只读 `/api/proactive/status` 的 mode=napcat、outbound_enabled=true 表示当前渠道可用，不保证此刻满足 Gate。

离线验证运行 `uv run pytest tests/test_proactive_delivery.py tests/test_outbound.py tests/test_proactive_review_fixes.py tests/test_napcat.py tests/test_reminder_delivery.py`。新增用例覆盖真实模式自动投递、来源/Conversation/Thread 隔离、关闭开关、等待期间插话/断线/会话切换、字面文本与真实回执格式、不确定终态、重连不补发、旧模拟数据保留，以及 schema 24→25 迁移回滚。原 Outbound 测试继续覆盖并发领取、频率限制、取消/超时、领取失败和发送后记账失败、进程退出恢复、CLI 模拟、Journal/Recall/学习隔离。

## 未来改进

用独立语义评测核对关注事项和草稿质量，区分渠道接受、用户已读与真实用户反馈，不把结构化离线测试当自然度证明。
