# Episode 与事后整理

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
