# Episode 与事后整理

Review Fix：schema 8 追加独立 [Dirty Thread Repair](thread-repair.md)。移动/解除关联后的旧 owner 不再依赖新 Episode 才能维护；`/thread repair` 分页重建当前实际成员，最终 CAS 才发布 clean state。无成员时宿主明确归档，不调用 LLM。

## Thread 独立组织层（schema 7）

`organization/threads.py::ThreadOrganizer` 处理已生成且 thread_dirty 的 Episode；`storage/threads.py::ThreadRepository` 按 Agent 保存最新 Thread、版本和有界批次来源。Thread 只描述持续话题及当前工作状态，memory_ref 保留宿主接口，模型不能写它，不创建 Semantic Memory。

实际链路：OrganizationRuntime 在 Idle 检查 Pending，四小时周期到期或默认累计 3 个 Episode 即调用 organize()；`/thread organize` 可立即触发，Dirty Repair 独立优先。snapshot() 读取至多 page_runs 个待处理 Episode、最近 24 个 Thread 及 Episode 的旧 owner。模型返回 groups/no_thread 完整分配；宿主校验 ID、恰好覆盖、单一归属、Thread 不重复分组。publish() 短事务复查 Thread/Episode/独立进度，原子更新 Thread 状态/修订、Episode 关联/dirty、thread_versions、thread_batches 与进度。旧 owner 先标 dirty；仍有未处理关联修订时不把 Thread 错标为最新。

优先匹配已提供的同一话题 Thread；闲聊可全 no_thread。active/paused/closed 的 Prompt 禁止仅因时间流逝关闭；失去全部关联时由宿主明确归档。last_active_at 由 Episode 真实时间计算，回填旧 Episode 不倒退时间。语义判断仍需真实评测。migration 7 增加原始 Thread 对象，8–10 增加失效/修复、时间调度与有限退避，原 Journal 不改。普通关联失败需显式 retry；旧 owner 的独立 Dirty Repair 不依赖新 Episode，按当前成员分页重建，完成前检索省略旧 state。

示例：`/episode organize` → `/thread organize` → `/thread list` → `/thread show <id>`；status 只显示状态。`tests/test_threads.py` 覆盖跨日聚合、闲聊、非法 ID、Revision 后重关联、事务回滚、取消/恢复、归属及 schema 6→7。

## 职责与架构组成

`organization/models.py` 定义严格 Pydantic 模型、来源快照和可信 OrganizationPolicy。`organization/provider.py::OrganizerModel.ask()` 每批仅调用一次原 Provider，空工具、独立输入/总输出 tokens、可见字节、超时和 usage 检查。`organization/episodes.py::EpisodeOrganizer` 生成时间候选边界、语义分段、验证证据。`storage/episodes.py::EpisodeRepository` 绑定 Agent，负责有界读取、归属、修订和原子进度。`organization/runtime.py::OrganizationRuntime` 在共享 CLI/QQ 应用存活时触发维护；独立 Web 不调度，`console.AsyncInput` 由 CLI 持有，只在 daemon 线程读取终端；SQLite 始终在主事件循环线程运行。

## 真实执行路径

1. `runtime.assemble_chat()` 复用同一 Provider/ContextBudget，构造 Repository、OrganizerModel、EpisodeOrganizer 和 OrganizationRuntime；recover() 把未提交的 running 整理恢复 pending，已提交 Episode 不重做。
2. chat() 启动时按持久 cursor 检查积压；_chat() await AsyncInput.read()，新输入先 await foreground() 取消并等待整理停止，再 Agent.run()。
3. 普通结果先展示，再 schedule()。原 MemoryAutomation 在可取消后台钩子内按原最多四次规则顺序执行，失败/取消仍采用原 schema 5 和显式 retry。Episode 在默认 1500 秒 Idle 或累计 8 条时获得处理机会；Idle 从完成/用户活动计量，不以 Run 开始时间代替。维护轮共享默认 4 批/4 请求/131072 tokens/120 秒，批间让出，冷却后继续积压，详见 [Schedule](organization-schedule.md)。
4. organize() → batch() 读取至多 page_runs 条元数据，以 SQLite rowid 插入顺序维护 cursor。running 停住；failed/cancelled/interrupted 不读取正文，作为屏障跳过并记 episode_skips。completed 先核对消息字节/数量，超 source_bytes 显式记录 oversized，不伪造经历。
5. 最近 provisional 仅在时间、数量和字节窗口内参与追加/重分段，首段复用 ID，新增拆分段生成新 ID；旧 finalized 不自动重写。不参与修订的尾部在发布事务 finalize。
6. 宿主按相邻 Run 的 started_at 差标注 continuous/weak/strong/required，默认 15/90/180 分钟，超过 3 小时及中断屏障强制拆分；午夜无特殊含义，开始时间倒序拒绝。候选同时给 start_gap_seconds 和 previous_duration_seconds，长工具执行不会伪造短输入间隔。最近 provisional 追加也用最后来源 Run 的 started_at 判断，而非整段 Episode 的最初时间或结束时间。投影保留结束时刻、Conversation/Run/sequence，不发送 Provider reasoning。
7. ask() → 严格 Segmentation；validate() 要求提供的 Run 恰好一次、同序覆盖、不跨屏障合并。Highlight 引文必须精确匹配所属原消息；confirmed 和 decision/correction 至少含用户证据。宿主证明引用存在，语义是否明确确认仍受模型质量限制。
8. publish() 短事务重新核对本批、provisional 修订和来源 hash，原子更新 Episode、归一化 episode_runs、追加 episode_versions、skip 和 cursor。失败回滚，原 Journal 不改。修订标记旧关联 Thread 待更新。
9. 无新历史仅检查 finalize。达到修订时长、容量、后续边界或排除 Run 时稳定归档；最后的小段可 provisional。退出/输入取消等待中的模型调用，不影响普通 Run。

## 持久化、异常与边界

稳定 Finalize 只改变 status/revision 时沿用 thread_dirty，避免语义内容未变却把 Thread state 标过期；重新分段、追加或摘要改变仍标记待更新。EvidenceRef 是组织器自己的小模型，允许单字精确引文，不继承 Reflection 的候选/审批语义。Thread status 只统计有界 ID，不反序列化私人摘要。

来源读取还验证完整 Run 的 sequence 连续性，避免用 first_sequence+index 假造不存在的引用。ToolResult 引文只能匹配模型实际看到的脱敏、至多 1024 字符投影；它不计作用户确认，decision/correction 仍必须有真实用户证据。对应测试在 tests/test_episodes.py。

migration 6 追加 episodes、episode_runs、episode_versions、episode_organizer_progress、episode_skips 和索引；同批创建空 topic_threads，以允许真实外键，行为由独立 Thread 服务承担。迁移不修改 1–5，不搬移旧 Run、Summary、Reflection 或消息。

episode_runs.run_id 主键防重复，Run/stream 和 Episode/stream 复合外键校验归属。versions 只存有界派生快照，不复制原始聊天。进度与 Compaction/Reflection 独立。模型、格式、来源或事务失败不推进 cursor，自动失败不付费重复，手动 organize 才 retry。等待模型无 SQLite 长事务。提交前崩溃可重算，提交后不重复；未知 usage 不当作零费用。纯派生重算不执行历史工具。

## 阅读与验证

`uv run personal-agent` 直接聊天；`/episode organize` 立即一批；`/episode status` 查看进度与用量；list/show 显式展示私人摘要。命令不创建 Run，不授予 Memory 权限。

配置前缀 AGENT_ORGANIZATION_：ENABLED、IDLE_SECONDS、RUN_THRESHOLD、PAGE_RUNS、INPUT_TOKENS、OUTPUT_TOKENS、TIMEOUT_SECONDS、WEAK_GAP_SECONDS、STRONG_GAP_SECONDS、SPLIT_GAP_SECONDS、REVISION_SECONDS、THREAD_THRESHOLD。窗口由 AGENT_CONTEXT_WINDOW 显式指定，不够则 input_budget。

最小路径见 `tests/test_episodes.py::test_provisional_append_split_revision_finalize_and_idempotence`；调度、CLI 输入、抢占、退出见 `tests/test_organization_runtime.py`。运行 `uv run pytest tests/test_episodes.py tests/test_organization_runtime.py`。FakeProvider 验证契约，不代表真实语义分段质量。
# Observatory 有界观测

`OrganizationRuntime._execute` 保留原调度，只在同一个 Stage await 前后记录最多 128 条无正文内存事件，取消继续传播。`diagnostics(stream_id, read_only=True)` 调用 `_observe`，用已有连接执行封顶 SELECT，不初始化 schedule、不推进学习、不调用模型。默认 CLI diagnostics 行为保持兼容。Dashboard 只消费这些投影；测试见 `tests/test_dashboard.py` 的只读诊断与实时 Stage 用例。


## 宿主边界

OrganizationRuntime 只管理现有后台 task、门控/公平性/定时、维护计量和内存事件；不创建终端线程、不解析 CLI、不执行观察 SQL。console.AsyncInput 与 cli_commands.MaintenanceCommands 由 main.chat 使用；ScheduleRepository.observe 管理观察查询。命令仍先 foreground()，不向模型或 HTTP 注册。tests/test_organization_runtime.py 验证构造无输入线程、诊断零写和前台抢占。

## 单次请求与大输出预算

每批 `EpisodeOrganizer.organize()` 仅调用一次 `OrganizerModel.ask()`，后者用 system 提示与 user JSON 发送一次无工具请求，再校验并发布；没有模型多轮循环或截断续写。积压分批和 Thread 整理各自是独立请求。整理 API 输出上限现在允许显式配置至 1,000,000 tokens（默认 8192），包含推理，可见 JSON 仍单独限制。1M 配置及窗口/维护预留关系见 [配置说明](config.md#显式-1m-整理配置)。

## 整理内容的语言

`organization/episodes.py::EPISODE_PROMPT` 要求标题、摘要及 Highlight 内容跟随来源对话的主要语言，中文讨论使用简体中文；混合语言按主要讨论判断，不因简短英文问候或技术名词改用英文，来源精确引文保持原文。`organization/threads.py::THREAD_PROMPT` 对标题和 current_state 使用同一原则，以所提供 Episode 代表的讨论为依据，不继承英文提示词或旧 Thread 文本的语言。`REPAIR_PROMPT` 同样约束根据当前关联 Episode 重建的 current_state。

规则由 `OrganizerModel.ask()` 随 system 提示发送，面板仍原样显示已保存内容；修改提示词不会自动翻译已存 Episode/Thread，也不触发重算或真实 API 调用。语言选择属于模型语义行为，现有离线流程测试验证分段、关联及修复契约，不代表语言质量的真实模型评测。验证路径见 `tests/test_episodes.py`、`tests/test_threads.py` 与 `tests/test_thread_repair.py`。
# P4 Shadow 维护单元

`OrganizationRuntime` 接受可选 `proactive` 服务；`_cycle()` 将其模型加入同一 `cycle_providers()`，Idle 时每轮最多运行一个 Proactive 单元，消耗原有 MaintenanceBudget。具体提取/判断/持久化均留在 proactive 子包，不由 Organizer 负责发送。`_background()` 将 Wake 的持久截止时间纳入等待，其他维护全关闭时 Shadow 仍可检查；前台抢占和退出保持同一 task.cancel()/await 路径。验证与边界见 [proactive.md](proactive.md) 和 `tests/test_proactive.py`。

# P4.3 草稿生成

ProactiveRuntime 发现已提交 notify 且尚无 Outbox 时，在下一有界维护单元调用 OutboundRuntime.compose()；Composer 复用已经由 cycle_providers 包装的同一个 model。不会在 Attention 内额外串联请求，也不由维护循环自动调用 Fake Sender。默认 `AGENT_PROACTIVE_COMPOSE_ENABLED=true`，仍接受原 Idle、冷却、前台取消和维护额度。异常、领取与取消路径见 [outbox.md](outbox.md) 及 `tests/test_outbound.py`。

Review Fix：Proactive 只在一个尚未领取其他维护单元的周期入口运行。若 learning_first（包含 Dirty Repair 的让出标记）生效，Learning 有待处理工作且其完整 requests/tokens 预留适合本轮配置，则 Proactive 让出周期，沿原调度顺序处理 Learning。周期结束继续使用原 learning_first 翻转，因此持续 Proactive 积压不会抢掉 Learning 所需的默认四次整批额度，也不会在 Learning 已消耗额度后再领取 Composer。若 Learning 配置本身无法容纳完整流水线，仍保留 learning_budget_config，且不阻止可执行的 Proactive。验证见 `tests/test_proactive_review_fixes.py`。

## 维护周期职责拆分

`OrganizationRuntime._cycle()` 只建立共享 Provider 预算、循环有界步骤、处理超时并提交周期统计；`_step()` 每步先异步让出控制并检查前台抢占，再选择 Proactive、回执恢复、Learning、Curator 或角色工作；`_organization_step()` 处理 Thread、Episode 与无模型 Finalization。`_learning_capacity()` 明确检查完整 Learning 管线能否容纳，保留 Learning-first 与脏线程让行及 Proactive 仅使用新周期的顺序。

`organization/cycle.py` 的 `CycleProgress` 保存单周期状态；`WorkResult.from_report()` 在服务边界把已有报告映射到完成、失败、停止、预算耗尽。只有 completed/finalized/repaired/repair_page 进入成功集合，待核对发布计为失败，空任务、stale、pending 和未知状态不再误记成功。模型与文件提交边界不变。验证：`tests/test_review_refactoring.py`、`tests/test_proactive.py`、`tests/test_organization_schedule.py`。
