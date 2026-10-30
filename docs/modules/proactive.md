# Proactive Shadow：机会、唤醒与 Attention

## 职责与架构组成

P4.1/P4.2 为现有应用增加未来关注事项，不增加长期事实存储、模型工具权限或外部发送能力。`proactive/models.py` 定义 Host 策略、真实用户来源、候选变更和 Attention 建议；`scout.py::OpportunityScout` 提取机会；`attention.py::AttentionEngine` 判断沉默、稍后检查、建议交流或建议任务。`wake.py::WakeCoordinator` 从持久截止时间计算检查间隔和安静窗口。`proactive/runtime.py::ProactiveRuntime` 编排一次有界检查，`storage/proactive.py::ProactiveRepository` 绑定 Agent，保存进度、Proposal 和审计。

当前只生成 follow_up 候选。外部事件、Social Window、Composer、Outbox/Delivery、明确提醒和自主工具任务留给后续阶段；没有后台发送接口。notify 和 propose_task 都只是 Shadow 建议，不表示已通知或获得任务授权。独立 Web 只读，不启动扫描或模型请求。

SQLite schema 16 追加 proactive_state、proactive_proposals、proactive_decisions。Proposal 是派生工作状态，用户经历以 Run Journal 为证据，长期理解仍以 MemoryFS Markdown 为权威。Shadow 不修改 Core、Deferred、Reflection 审批或 Character Package。

## 代码实际运行路径

1. `runtime.open_application()` 初始化 schema；`assemble_chat()` 复用 Provider、ContextBudget 和当前 Agent，构造 Repository、OrganizerModel 与 ProactiveRuntime，交给 OrganizationRuntime。启用时首次 initialize() 把当前 Run/Thread 水位作为基线，不默认扫描旧历史；重启保留水位。observer_only 不初始化。
2. 非只读 `open_chat()` 在启用时安排现有维护任务，进程空闲也能检查，不要求先有新用户消息。CLI/QQ 展示回答后继续通过 ChatService.after_reply() 唤醒；通知不代表某个 Run 已处理。Scout 始终按数据库水位发现证据。
3. `OrganizationRuntime._background()` 使用现有 Clock 和单个任务等待，间隔取维护检查、Idle、冷却与 WakeCoordinator.delay() 的较小值。Wake 最多每分钟查询元数据，有候选时参考最早 review/expiry，没有每项独立计时器。执行仍受 Idle、维护冷却及预算约束，所以 Shadow 不保证精确时间提醒。
4. `_cycle()` 在相同 MaintenanceBudget/cycle_providers 串行作用域内，每轮最多执行一个 Proactive 单元，然后让其余维护阶段继续使用原有批次/请求/tokens/时间额度。ready() 只检查元数据。新输入通过 foreground() 取消并等待整个维护任务；没有第二条并发后台模型通道。关闭 P1/P2/P3 后，启用的 Shadow 仍可运行。
5. `ProactiveRuntime.run()` 先有界终结过期事项，随后优先处理未处理来源。snapshot() 每次读取一个 Run，最多 64 条消息且完整 payload 合计不超过 source_bytes；只向模型提供实际 user 文本，不提供助手文本、工具参数/结果、图片正文或 Provider JSON。running 是屏障，failed/cancelled/interrupted 和过大来源不调用模型，明确记录跳过并推进。
6. Run 处理完后增量读取一个 Thread 版本。Thread 仅作线索，证据取当前关联的至多四个、启用后 completed Run；来源更多时记 thread_source_budget，不把局部片段冒充完整证据。旧版本和 dirty Thread 被跳过。用户来源回到 Journal，有独立字节检查，不将派生摘要本身作为引文。
7. `OpportunityScout.extract()` 通过 OrganizerModel.ask() 发起一次空工具请求。Host 校验严格 JSON、重复 topic_key、每条引文的 Conversation/Run/sequence 和精确用户文本。状态只允许 open/resolved/dismissed；没有事项可返回空数组。摘要指令不能授予权限。
8. `publish()` 短事务复核水位和 Thread 修订，原子提交候选、审计和水位。同一 Agent 的 topic_key 唯一，旧来源不能覆盖较新来源；取消/完成保留终结记录，旧摘要不得重新打开。关联 Thread 的变更也推进 Proposal Revision。新用户计划可以用更新来源重新开启。最多 48 个活跃事项，满额时明确审计 active_capacity 并跳过新增，继续推进以便后来的取消仍可处理；模型看到活跃事项与最近 16 个关闭事项的简短标识，语义匹配依赖模型，不宣称完全消除近义重复。
9. 没有未处理输入时，Eligibility Gate 检查开关、失败状态、正在运行的用户请求和安静时段；Attention 只看到最多 candidate_limit 个到期且未过期候选。evaluate() 必须选择一个已提供 ID。Host 再核对最新输入、开关、Proposal Revision 与有效状态，才写入审计。QQAdapter.accept() 在持久收件后立即调用 ChatService.note_input()，即使尚未形成 Run，也使正在计算的 Attention 过期。
10. silent/notify/propose_task 检查后不再对不变证据重复评估，事项仍保持 open；新证据可以更新。defer 至少等待 review_seconds，且不超过 expiry。审计保存判断、候选修订和 Shadow 标记，不推断用户偏好或隐式完成。有效期按最新引用用户证据时间加 lifetime_seconds，重启或旧摘要不会延长。

## 配置与可信入口

库级与示例默认 AGENT_PROACTIVE_ENABLED=true，用户明确要求默认开启 Shadow。后台使用已配置 Provider，受现有维护预算限制；设为 false 可关闭。开发验证全部离线。配置沿用当前项目 .env/环境变量，不在其他路径搜索凭证。Luna 由 AGENT_CHARACTER_ID=luna 显式选择，库级角色默认 none。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| AGENT_PROACTIVE_INPUT_TOKENS / OUTPUT_TOKENS | 16384 / 4096 | 单次输入/总输出预算 |
| AGENT_PROACTIVE_TIMEOUT_SECONDS | 30 | 模型请求超时 |
| AGENT_PROACTIVE_SOURCE_BYTES | 32768 | 反序列化前完整来源 payload 上限 |
| AGENT_PROACTIVE_REVIEW_SECONDS | 21600 | 默认关注延迟及 defer 最小间隔 |
| AGENT_PROACTIVE_LIFETIME_SECONDS | 604800 | 用户证据之后的有效期 |
| AGENT_PROACTIVE_QUIET_START_HOUR / END_HOUR | 23 / 8 | 安静窗口，相等表示关闭 |
| AGENT_PROACTIVE_UTC_OFFSET_MINUTES | 480 | 固定 UTC+8 偏移；当前不支持夏令时规则 |
| AGENT_PROACTIVE_CANDIDATE_LIMIT | 3 | 单次输入候选数，最多四个 |

CLI 的 `/proactive status|proposals|decisions` 只读；`/proactive run` 在相同维护预算内执行一个单元；`/proactive retry` 显式解除失败暂停，下次重新读取当前证据，不重放旧模型输出。这些入口不注册为模型工具。QQ 自然语言不能修改开关或调用管理命令。

Observatory 的 `/proactive` 页面及 `/api/proactive/status`、`/api/proactive/proposals`、`/api/proactive/decisions` 只读。列表按 rowid 游标分页，每页最多 20；复用 Agent 隔离、loopback Host/Origin 防护、隐私过滤和响应上限。页面逐页展示来源、状态与判断原因，不自动加载全部私人历史。

## 异常与边界

- 无效模型 JSON、工具调用、伪造引文和来源发布失败暂停提取水位，保存固定错误码，必须显式 retry。单次 Attention 失败暂停该次候选，retry 或更新用户证据才可再评估。日志不打印私人文本或推理。
- 维护预算不足不推进来源，后续周期可以读取当前证据，不重试工具或外部副作用。结构性数据库失败向上停止维护。候选、水位和审计同事务，提交失败回滚；CAS 拒绝旧水位再次发布。
- OrganizerModel 拒绝超时或吞掉取消后返回的结果。Attention 在 await 后复核输入和版本，过期建议不记为已采用。
- 应用关闭时没有后台执行。重启只恢复派生进度与有界检查，不保证停机期间准确到期执行。当前没有消息投递，因此没有送达、已读或重复投递保证。
- 不扫描所有 MemoryFS、不默认历史补学。候选语义与价值判断需要后续真实审阅，离线契约测试不能证明自然度。

## 阅读与验证

最小离线验证：`uv run pytest tests/test_proactive.py`。测试建立临时 SQLite 和 FakeProvider，启用后写入 completed 用户 Run，再执行 Scout 和 Attention，验证只有派生状态与审计，没有额外 Run、工具或发送。

`tests/test_proactive.py` 覆盖 Run/Thread 增量、空结果、重启水位、取消与旧摘要竞争、助手引文拒绝、跨 Agent、未完成 Run、过大来源、安静窗口、失效、Attention 动作、新 Run/QQ 收件版本/Revision 竞争、吞取消、共享预算、其他维护全部关闭后的定时检查、schema 15→16 原子迁移和回滚、发布失败回滚及 Observatory 只读分页。项目完整验证还包括 pytest、Ruff、打包和 CLI 启动检查。
