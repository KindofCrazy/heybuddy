# Organization 时间调度

P2 Review Fix：每个 Idle 维护轮可先执行一个 memory_recovery 单元，经原 foreground/run_active 门控、批次数和整轮时间预算；它仅核对本地 hash/索引并保存恢复状态，0 次模型请求。确认后普通学习仍在原完整流水线请求预算中运行；没有另起恢复 Loop。恢复失败的持久退避与次数见 [Learning](learning.md)，tests/test_learning_recovery.py 覆盖前台阻止、Idle 自动核对和后续学习。

P3 在同一维护轮加入可选 CharacterStateService：仅 Idle、新角色来源、最小间隔和共享剩余额度允许时执行一次空工具建议。character_ran 限制一轮最多一次；P2 获得完整学习预算的优先轮时 State 等待。CycleProvider 统一扣减 State 的输入/输出预留与 usage；foreground() 取消并等待 pending 记账，State 永远不产生聊天输出。真实分支与取消/预算测试见 [State](character-state.md)、tests/test_character_state.py。

P2 在同一维护循环加入 MemoryLearningService/MemoryCurator，复用 Clock、MaintenanceBudget、前台门控和冷却，不另起 Scheduler。Priority 入队不发 Classifier，回复后尽快后台执行；Batch 在 Idle 等 finalized Episode 数量/最大等待；Curator 低频且有变化才调用。默认维护轮 4/4/131072/120 同时覆盖 Episode/Thread/Repair/Learning/Curator。学习按整条流水线保守预留，和其他维护交替；Dirty 先执行后允许让出整轮给等待中的学习，避免占满请求饿死 P2。P2 enabled 时不执行旧 MemoryAutomation；disabled 才沿用下文原模式。来源过大保存 failed，不停止其他维护。tests/test_learning_runtime.py、test_p2_integration.py、test_p2_boundaries.py 覆盖双速触发、持续积压公平性、取消与恢复。

## 职责与组成

`OrganizationRuntime` 是唯一的本地维护循环，统一 Episode、Finalize、Thread 和 Dirty Repair；没有通用任务框架。`organization/clock.py::Clock/SystemClock` 提供 UTC wall time、monotonic 和可取消 sleep。`storage/schedule.py::ScheduleRepository` 保存一个 Agent 级调度行；schema 9 追加 organization_schedule，原迁移 1–8 不改，Journal 不改。Run/关联/修复进度仍在原独立表。

## 真实执行顺序

1. chat() 启动调用 schedule()。ensure() 恢复持久 deadline；首次根据最近终结 Run 的 ended_at 恢复活动时间，Thread 周期参考待处理 Episode 的时间，无历史时从现在开始。schedule() 对同一个存活 task 幂等，不创建并行 Organizer。
2. 新输入先 foreground()：置前台标志、取消并 await 后台任务、记录用户活动。普通 Run/命令期间没有后台维护；回答展示后 schedule() 记录完成时刻，再允许维护。空白输入也重置活动。close() 取消而不制造新活动，重启保留原 deadline。
3. `_background()` 先顺序执行原 MemoryAutomation（若本轮 completed），其取消/审批语义不变，再运行维护检查。state() 只读元数据；发现 running Run 时不发模型请求。
4. `_idle_remaining()` 在启动时从持久 activity/最近 Run ended_at 恢复空闲年龄，之后用 monotonic 计量，避免 wall clock 跳跃凭空制造 Idle。`_thread_remaining()` 和 `_cooldown_remaining()` 同样将持久 UTC deadline 映射为本进程 monotonic deadline，避免回拨延误周期或积压冷却。新活动/终结 Run 重置 Idle 基准，Thread 检查/成功更新独立周期。
5. `_cycle()` 在 Idle 时优先 Dirty Repair，到期或数量达标的 Thread 次之；已到期 provisional Finalize 无模型执行，即使有失败 Pending 批次也能结束修订窗口。Episode 在 Idle 默认 1500 秒或数量默认 8 时整理。时间驱动只调度已有工作，不机械创建 Episode。
6. Thread 有 Pending 且周期默认 14400 秒到期，或数量默认 3 时获得整理机会；普通 Thread 在 Idle 执行。周期到了但无 Pending 只推进下一次检查，不调用模型。Dirty Repair 不等 4 小时，不依赖新 Episode。
7. 每轮最多默认 4 个维护单元、4 次模型请求、131072 预留 tokens、120 秒。`organization/budget.py::MaintenanceBudget/CycleProvider` 对 Episode/Thread/Repair 共享预留账本，调用前拒绝超限；原 Provider 在 finally 恢复。每批 await sleep(0) 让出事件循环，Dirty 优先、到期/数量达标的 Thread 先获得机会，再处理 Episode，避免大量 Runs 饿死 Thread。每轮结束持久保存 cooldown（默认 60 秒）及计量；持续 Idle 时自动开始下一轮，不等新消息。
8. 预算耗尽返回 budget_exhausted，当前来源保持 pending，之前提交保留；单请求根本无法放入轮 token 上限时明确失败，不能每分钟无限重发。整轮 timeout 取消当前请求并保留检查点，输入取消同样先清理再前台。Episode/普通 Thread 模型失败仍需手动 organize；Repair 短暂失败按持久指数退避重试（默认 300 秒起），同源连续默认三次失败后保留 dirty、错误/重试信息，`/thread repair` 显式重新给出尝试机会。新来源 epoch 会重置旧失败。schema 10 仅追加 failure_count/retry_after/cycle_metrics，不改 1–9。

## 配置、诊断与边界

AGENT_ORGANIZATION_IDLE_SECONDS、RUN_THRESHOLD、REVISION_SECONDS、THREAD_INTERVAL_SECONDS、THREAD_THRESHOLD、CHECK_SECONDS、COOLDOWN_SECONDS 均经 Settings/OrganizationPolicy 有界校验。时间触发只决定执行时机，语义分段仍由 Episode Organizer 决定。

`/episode status`、`/thread status` 的 schedule 显示持久 activity、thread_due_at、next_idle_at、next_finalize_at、最近各类成功时间、last_batches/last_requests/last_error、前台标志、后台 task 状态与实际 current_stage；后台 task 在等待定时器不等于正在请求模型。Thread status 另含 dirty 数量及最近 repair 错误。status 只读数据/维护元数据，不触发 LLM。

调度只在进程存活时运行；重启依据持久时间和独立来源进度继续，没有系统级服务。同步磁盘不是可硬取消 I/O。正常模型/工具不受维护成功与否影响；原 MemoryAutomation 的失败仍需原显式 retry。

OrganizerModel.ask() 在 Provider 返回后重验 deadline/task cancellation，即使 Provider 吞掉 CancelledError，也不发布晚到的建议。进程重启后的时间恢复仍依赖可靠的宿主 UTC 时钟；monotonic 不能修复跨进程的任意时钟损坏。

额外配置 AGENT_ORGANIZATION_MAINTENANCE_BATCHES/REQUESTS/TOKENS/SECONDS、REPAIR_RETRY_SECONDS/MAX_FAILURES 均有上下限。cycle_metrics 仅保存数值与标志，不含正文；reasoning 作为 output 子集单列，不重复计费，未知 usage 不当零费用。has_backlog、current_stage、失败次数和 retry_after 能区分等待冷却、维护中、待继续与需人工 retry。

## 阅读与验证

`tests/test_organization_schedule.py::FakeClock` 可控推进时间，不等待真实 25 分钟/4 小时。覆盖一条 Run、长 Run、running 抑制、四小时单 Episode、空周期无请求、Finalize、Dirty 优先、wall clock 跳跃、活动重置、重复 schedule 和持久 deadline 恢复。结合 `tests/test_organization_runtime.py` 与 `tests/test_p1_recovery.py` 验证真实 CLI 抢占/退出。

`tests/test_organization_backlog.py` 覆盖 40 Runs 分批/跨请求预算继续、冷却不提前调用、Store 重启不重复、第二批取消保留第一批、Thread backlog、整轮 timeout、不可能容纳的请求、Repair 退避/连续失败上限、运行时抢占及重建检查点/失败 deadline 重启恢复。`tests/test_p1_recovery.py` 保留真正子进程硬退出验证。

`tests/test_review_integration.py` 验证连续活跃不按时钟机械分段、Provider 吞取消不提交、系统时钟回拨不延迟当前周期或积压冷却、Finalize 不被失败批次卡住，以及真正 os._exit 后 Idle 自动恢复 Repair 已提交页。

## 查询与预算作用域

ScheduleRepository.state() 在无持久行时只返回推导视图，diagnostics() 不再 ensure；observe() 提供 Dashboard 的有界只读投影。只有 schedule()/真实维护才初始化或推进元数据。cycle_providers() 是现有串行维护/手动命令共用的同步作用域，按 model 身份去重，finally 恢复 Provider；共享 MaintenanceBudget 和既有调度顺序不变。
