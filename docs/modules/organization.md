# Episode 与事后整理

## Thread 独立组织层（schema 7）

`organization/threads.py::ThreadOrganizer` 处理已生成且 thread_dirty 的 Episode；`storage/threads.py::ThreadRepository` 按 Agent 保存最新 Thread、版本和有界批次来源。Thread 只描述持续话题及当前工作状态，memory_ref 保留宿主接口，模型不能写它，不创建 Semantic Memory。

实际链路：OrganizationRuntime 完成一批 Episode 后查 pending_episodes_capped，默认达到 3 才调用一次 organize()；`/thread organize` 可立即触发。snapshot() 读取至多 page_runs 个待处理 Episode、最近 24 个 Thread 及 Episode 的旧 owner。模型返回 groups/no_thread 完整分配；宿主校验 ID、恰好覆盖、单一归属、Thread 不重复分组。publish() 短事务复查 Thread/Episode/独立进度，原子更新 Thread 状态/修订、Episode 关联/dirty、thread_versions、thread_batches 与进度。旧 owner 先标 dirty；仍有未处理关联修订时不把 Thread 错标为最新。

优先匹配已提供的同一话题 Thread；闲聊可全 no_thread。active/paused/closed 的 Prompt 禁止仅因时间流逝关闭。last_active_at 由 Episode 真实时间计算，回填旧 Episode 不倒退时间。语义判断仍需真实评测。migration 7 只追加独立状态、版本、批次和 pending 索引，原 Journal 不改。退出按 dirty 集合重算，提交后不重复；失败阻止自动付费重试，手动 organize 才 retry。旧 Thread 移出 Episode 后可能保持 dirty，检索层应省略其旧 state，后续相关 Episode 再刷新，不自动全库重算。

示例：`/episode organize` → `/thread organize` → `/thread list` → `/thread show <id>`；status 只显示状态。`tests/test_threads.py` 覆盖跨日聚合、闲聊、非法 ID、Revision 后重关联、事务回滚、取消/恢复、归属及 schema 6→7。

## 职责与架构组成

`organization/models.py` 定义严格 Pydantic 模型、来源快照和可信 OrganizationPolicy。`organization/provider.py::OrganizerModel.ask()` 每批仅调用一次原 Provider，空工具、独立输入/总输出 tokens、可见字节、超时和 usage 检查。`organization/episodes.py::EpisodeOrganizer` 生成时间候选边界、语义分段、验证证据。`storage/episodes.py::EpisodeRepository` 绑定 Agent，负责有界读取、归属、修订和原子进度。`organization/runtime.py::OrganizationRuntime` 仅在 CLI 存活时触发整理，`AsyncInput` 只在 daemon 线程读取终端；SQLite 始终在主事件循环线程运行。

## 真实执行路径

1. `main._run_session()` 复用同一 Provider/ContextBudget，构造 Repository、OrganizerModel、EpisodeOrganizer 和 OrganizationRuntime；recover() 把未提交的 running 整理恢复 pending，已提交 Episode 不重做。
2. chat() 启动时按持久 cursor 检查积压；_chat() await AsyncInput.read()，新输入先 await foreground() 取消并等待整理停止，再 Agent.run()。
3. 普通结果先展示，再 schedule()。原 MemoryAutomation 在可取消后台钩子内按原最多四次规则顺序执行，失败/取消仍采用原 schema 5 和显式 retry。Episode 在累计默认 8 条或距上次输入默认 1500 秒时触发。每次最多一批，之后最多一次无模型 finalize；没有无界消费循环。
4. organize() → batch() 读取至多 page_runs 条元数据，以 SQLite rowid 插入顺序维护 cursor。running 停住；failed/cancelled/interrupted 不读取正文，作为屏障跳过并记 episode_skips。completed 先核对消息字节/数量，超 source_bytes 显式记录 oversized，不伪造经历。
5. 最近 provisional 仅在时间、数量和字节窗口内参与追加/重分段，首段复用 ID，新增拆分段生成新 ID；旧 finalized 不自动重写。不参与修订的尾部在发布事务 finalize。
6. 宿主按相邻 Run 的结束/开始时间标注 continuous/weak/strong/required，默认 15/90/180 分钟，超过 3 小时及中断屏障强制拆分；午夜无特殊含义，时间倒序拒绝。投影带 Conversation/Run/sequence/真实时间，不发送 Provider reasoning。
7. ask() → 严格 Segmentation；validate() 要求提供的 Run 恰好一次、同序覆盖、不跨屏障合并。Highlight 引文必须精确匹配所属原消息；confirmed 和 decision/correction 至少含用户证据。宿主证明引用存在，语义是否明确确认仍受模型质量限制。
8. publish() 短事务重新核对本批、provisional 修订和来源 hash，原子更新 Episode、归一化 episode_runs、追加 episode_versions、skip 和 cursor。失败回滚，原 Journal 不改。修订标记旧关联 Thread 待更新。
9. 无新历史仅检查 finalize。达到修订时长、容量、后续边界或排除 Run 时稳定归档；最后的小段可 provisional。退出/输入取消等待中的模型调用，不影响普通 Run。

## 持久化、异常与边界

migration 6 追加 episodes、episode_runs、episode_versions、episode_organizer_progress、episode_skips 和索引；同批创建空 topic_threads，以允许真实外键，行为由独立 Thread 服务承担。迁移不修改 1–5，不搬移旧 Run、Summary、Reflection 或消息。

episode_runs.run_id 主键防重复，Run/stream 和 Episode/stream 复合外键校验归属。versions 只存有界派生快照，不复制原始聊天。进度与 Compaction/Reflection 独立。模型、格式、来源或事务失败不推进 cursor，自动失败不付费重复，手动 organize 才 retry。等待模型无 SQLite 长事务。提交前崩溃可重算，提交后不重复；未知 usage 不当作零费用。纯派生重算不执行历史工具。

## 阅读与验证

`uv run personal-agent` 直接聊天；`/episode organize` 立即一批；`/episode status` 查看进度与用量；list/show 显式展示私人摘要。命令不创建 Run，不授予 Memory 权限。

配置前缀 AGENT_ORGANIZATION_：ENABLED、IDLE_SECONDS、RUN_THRESHOLD、PAGE_RUNS、INPUT_TOKENS、OUTPUT_TOKENS、TIMEOUT_SECONDS、WEAK_GAP_SECONDS、STRONG_GAP_SECONDS、SPLIT_GAP_SECONDS、REVISION_SECONDS、THREAD_THRESHOLD。窗口由 AGENT_CONTEXT_WINDOW 显式指定，不够则 input_budget。

最小路径见 `tests/test_episodes.py::test_provisional_append_split_revision_finalize_and_idempotence`；调度、CLI 输入、抢占、退出见 `tests/test_organization_runtime.py`。运行 `uv run pytest tests/test_episodes.py tests/test_organization_runtime.py`。FakeProvider 验证契约，不代表真实语义分段质量。
