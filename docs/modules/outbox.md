# Composer 与离线 Outbox / Delivery

关心式窗口的来源读取：`OutboxRepository._proposal()` 同时解析 schema 20 的 followup_window，供 Composer 看到 before/evening 阶段；原来源、Revision、epoch 与有效期复核不变。明确提醒的真实 NapCat Sender 仅接在独立 Scheduler，当前 Proactive DeliveryGrant 和 FakeSender 仍只有 simulation，不复用该真实发送许可。窗口路径见 [proactive.md](proactive.md)，提醒投递见 [scheduler.md](scheduler.md)。

## 职责与架构组成

P4.3 第一阶段把 Attention notify 转成 Luna 口吻的草稿，并提供显式本地模拟投递。`proactive/composer.py::Composer` 复用原 OrganizerModel，严格 MessageDraft JSON、空工具、原输入/输出/超时预算；`proactive/outbound.py::OutboundRuntime` 负责生成前后和投递前的 Host 复核。`proactive/delivery.py` 定义不可变的 DeliveryGrant、SendReceipt、Sender Protocol 和完全内存实现的 FakeSender；没有注册真实渠道 Sender，没有自动模拟投递。

`storage/outbox.py::OutboxRepository` 绑定 Agent，保存草稿、发送尝试、终态和重启恢复。schema 17 原子追加 proactive_outbox 与 proactive_attempts，不修改旧迁移和原 Journal。每个 Agent/Decision 最多一个草稿，每个 Outbox ID 最多一次发送尝试，Outbox ID 同时用作幂等键。状态分开表示 composing、prepared、compose_failed、sending、accepted、failed、uncertain、cancelled；accepted 只表示 Fake Sender 接受，不表示真实送达或用户已读。

## 代码实际运行路径

1. `runtime.assemble_chat()` 在 Agent 构造前绑定 SessionManager.notice_factory。它按当前 Conversation 建立 OutboundContext；默认不读取模拟消息。随后建立现有 ProactiveRuntime，并把 OutboundRuntime 接在其 outbound 属性上。角色 callback 从当前只读 CharacterContextCompiler 编译口吻，保存 Character Package fingerprint；无角色时使用 none。observer_only 不执行 recover。
2. 原 Attention notify 已经提交后，`ProactiveRuntime.ready()/run()` 在没有未处理用户输入、符合原 Eligibility Gate 时，发现尚无 Outbox 的 notify 决策。下一维护单元调用 outbound.compose()，与 Scout/Attention 共用同一个 model、MaintenanceBudget 和可抢占维护 task，不在原 Attention 请求内串联额外请求。
3. `OutboxRepository.begin()` 验证实际 Decision/Agent、唯一候选和决策对应修订（Attention 提交后的 revision）。重新核对 Proposal 状态、有效期、实际 user 引文、原 Run completed 和归属，再原子领取 composing 草稿。保存当前会话、最新 Run/Message 水位、QQ 收件 epoch、角色 fingerprint。旧决定失效则保存 cancelled，不调用 Composer。目标会话只由 Host callback 决定。
4. `Composer.compose()` 以角色口吻、当前时间和带来源的 Proposal 起草，输出至多 1200 字符。提示词禁止虚构用户关系、任务成功或提醒已设置。OrganizerModel 检查超时、取消、工具调用、JSON、字节、tokens 与凭证模式；取消后返回的输出也不能发布。
5. await 后 `OutboundRuntime._valid()` 重读 Gate、输入水位、收件 epoch、当前会话、角色 fingerprint、Proposal Revision 和有效期。prepare() 再次 CAS 检查 composing 状态和来源，然后写 prepared。草稿有效期不超过 Proposal expiry 和生成领取后一天，重启不延长。无效结果改 cancelled；模型失败改 compose_failed，不自动再生成。
6. `/proactive simulate <outbox_id>` 是可信 CLI 的明确本地模拟命令。MaintenanceCommands 创建本地 FakeSender 和绑定当前会话/模拟收件人的 DeliveryGrant；模型和 HTTP 都没有此执行入口。授权默认 disabled，只有这个显式命令或测试 Harness 传入 enabled 的模拟许可，才可能进入 claim。当前实现拒绝 mode 不为 simulation 的 Sender。
7. simulate() 两次读取不可变授权，检查许可版本未变，并在 claim 前复核最新 Host 状态；claim() 短事务检查草稿未被其他调用领取、来源与会话仍有效，按 Agent/recipient 计算最近一天尝试数与冷却。默认每天最多两次、间隔六小时，unknown 也计入额度。事务同时保存 sending 和唯一 attempt，提交完成后才第一次 await Sender。
8. FakeSender 只写内存字典，幂等键相同但内容不同会明确失败。成功后 finish() 在同一事务提交 Outbox/Attempt accepted 和模拟 receipt_id。明确发送失败保存 failed；超时、未确认结果、意外异常或取消保存 uncertain。没有终态到 prepared 的投递重试接口，也没有自动重发。
9. recover() 在应用启动、持有原数据库锁后执行：sending → uncertain，composing → compose_failed，prepared → cancelled（需重新核对）。accepted/failed/uncertain 不重做。若 Fake Sender 已产生效果而回执写库失败，保留 sending；重启只能标 uncertain，不能声称撤销。恢复不调用模型或 Sender。
10. `/proactive retry-compose <id>` 是显式生成重试。只接受 compose_failed，重新核对当前会话、原 Proposal Revision、用户证据与未过期状态，绑定当前输入和角色，再生成新的草稿；不复用失败正文，不延长原有效期。已发送、不确定、已取消或来源变更的条目不能以此重做。

## 对话接续与学习隔离

后续实测补充 Composer 的自然度约束：通过少问、少打扰体现克制，不主动把“不催你”、边界声明或“按你的节奏”念成安抚模板；不添加未被用户引文支持的当面/线上形式、地点、建议或结果预期。该规则只约束 text 内容，外层 MessageDraft JSON 和领取/复核流程不变。模型仍可能忽略语义约束，结构检查通过不等于自然度或事实完全正确。

`Composer.compose()` 接收角色风格时剔除末尾精确匹配的通用主对话尾文，避免把主对话分条/终止规则重复带入结构化生成和输入预算；角色内容保留，原始入参不变。预算上限不扩大，集成回归见 `tests/test_flow_audit.py` 与 `tests/test_outbound.py`。

`Composer.PROMPT` 要求草稿通常一两句，遵循用户最新澄清，不自动给清单、追加追问或要求汇报。没有用户求建议的来源时，不为凑交流价值提供服务邀约；贴合事项的简短祝愿即可。句数约束只作用于 JSON 的 text 字段，外层始终为符合 MessageDraft 的 JSON 对象，不能裸输出正文。该提示词修订不改变 Gate、证据复核、草稿领取或真实发送边界；隔离 API 测试只生成合成草稿，见 [评测说明](evaluation.md#历史问题驱动的-prompt-复核)。

`core/outbound.py::ConversationNotices` 是独立只读 Protocol。ContextBuilder._injections() 可读取一个带 kind=proactive_messages、origin=proactive、trust=assistant_generated_only、user_evidence=false 的低信任 JSON；该注入计入原上下文预算，不进入 pending 或 messages。数据额外标明每条消息的 assistant 角色，原冻结 system 提示词保持不变。SessionManager.context() 通过 notice_factory 将读取器绑定到当前 Conversation，切换会话不会继续读取旧会话。

当前只有 Fake Sender，所以生产 OutboundContext 默认返回 None。离线 Harness 必须显式 include_simulations=True，才读取当前 Agent/Conversation 最近至多三条 accepted 模拟消息，并标记 simulation=true。prepared、failed、uncertain、其他 Agent/Conversation 都不进入上下文。可在 Fake Provider 请求中看到草稿后再输入“还没做完”，验证接话，不冒充真实投递。

Outbox 不写 Run、不伪造 user 消息，不进入 Recall 派生索引、Episode 原始来源或 Scout 用户证据。Reflection/P2 继续只读取原有真实来源，不向其提供主动草稿或模拟回执；原有精确引文及 confirmed 用户证据校验和冻结提示词保持不变。用户自己实际说出的内容仍保留正常证据资格，模糊回复不能自行证明用户兴趣。未来接入真实渠道时，须新增经过验证的投递能力和真实 notice 读取路径，不能把模拟记录升级成真实消息。

## 入口、配置与异常边界

Review Fix：Proposal.revision 表示影响证据、事项状态及执行资格的语义修订；仅补齐 thread_id 时记录 linked_thread/metadata_only 审计，不递增它。因此正常后台关联不会使 notify 决策或 prepared 草稿无故失效。新用户更正、取消、完成和到期仍递增修订并阻止旧草稿。已 accepted/uncertain 的条目仍不能重发；历史上已经 cancelled 的草稿也不因本次修复自动恢复。新增回归位于 `tests/test_proactive_review_fixes.py`。

Shadow 与 Composer 预览默认开启，`AGENT_PROACTIVE_ENABLED=false` 关闭整个流水线，`AGENT_PROACTIVE_COMPOSE_ENABLED=false` 仅关闭草稿生成。Composer 消耗原维护额度，真实调用可能计费；本次验证全部 Fake Provider，不进行真实模型或 QQ/NapCat 登录。没有真实发送开关。

CLI 可读 `/proactive outbox`、`/proactive attempts`，原 run 一次只执行一个有界单元；retry-compose 也受同一 MaintenanceBudget 约束。simulate 只调用本地 FakeSender，不请求模型。Observatory `/proactive` 增加消息草稿和模拟尝试，`/api/proactive/outbox|attempts` 继续仅 GET/HEAD、Agent 隔离、有界游标分页、隐私过滤与响应上限。

数据库读取、领取或发布失败向上停止当前单元；领取事务失败保证 Sender 尚未调用。模型失败保留固定错误码，不打印异常体、私人草稿或推理。取消是协作式：网络提交一旦开始，之后撤销许可不能撤回已经产生的效果；当前只在内存模拟这一边界。没有任务执行、提醒调度或 Social Window。

## 阅读与验证

最小验证为 `uv run pytest tests/test_outbound.py tests/test_proactive.py`。PipelineProvider 离线返回一个用户计划、一条 notify 和一份 Luna 草稿，FakeSender 通过明确模拟许可接受；再用单独 FakeProvider 回答“还没做完”。不创建额外 Run 或长期记忆。

`tests/test_outbound.py` 覆盖口吻和证据装配、生成/投递分离、幂等和终态去重、User/Agent/Conversation 隔离、禁用与撤销许可、输入/QQ epoch/角色/Revision/expiry 竞争、模拟上下文接续和 Journal/Recall/Scout 隔离、明确失败/取消/超时/吞超时、发送后写库失败、领取事务回滚、重启冻结、显式重新生成、收件人频率限制、CLI 模拟与 HTTP 只读，以及 schema 16→17 的原子迁移和失败回滚。完整项目回归仍运行 pytest、Ruff、构建和 CLI 启动检查。
