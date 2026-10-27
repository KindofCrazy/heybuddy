# Dirty Thread Repair

## 职责与架构组成

`storage/maintenance.py::CONSISTENCY_SCHEMA` 是追加 migration 8，不修改 1–7。`topic_threads.source_revision` 随 Episode 移入、移出、删除及摘要/来源变更递增；触发器同时标记旧/新 owner dirty。仅 status Finalize 不改变语义内容时不触发失效。`organization/models.py::RepairPage/ThreadRepair` 定义有界来源页与严格输出；`storage/thread_repair.py::ThreadRepairRepository` 管理重建检查点、归一化来源与最终发布。`ThreadOrganizer.repair()` 复用 Provider、输入/输出上限和同一 asyncio.Lock，与普通关联整理串行。

## 真实执行路径

1. `/thread repair` → OrganizationRuntime.command() → ThreadOrganizer.repair()；pending() 查询当前 Agent 的一个 dirty owner，无需新增 Episode。
2. window() 比较 Thread 状态 revision/source_revision 和已有 job。首次或关联变化时清空本次重建工作摘要及 scratch 来源，从当前关联的最早 Episode 开始，绝不把旧 current_state 当作重建起点。
3. `_page()` 按 ended_at/id 查询至多 page_runs+1 条当前仍关联的 Episode 元数据，先检查来源字节，再读取有界正文。返回 working_state、cursor、来源数、完整 Episode revision 和 has_more；等待模型时不持有事务。
4. 有来源时 OrganizerModel.ask(ThreadRepair, REPAIR_PROMPT) 仅接收当前页与本轮重建的前页工作摘要。已移走的 Episode 和旧 Thread state 不进入输入。无来源时不调用模型，由宿主记录“当前没有关联的 Episode。”并 closed。
5. publish() 在短事务复读 Thread/source_revision、cursor、当前来源页，冲突拒绝。中间页只保存有界工作摘要和 thread_repair_sources，Thread 保持 dirty，不发布半成品。最后一页原子更新 Thread revision/state/dirty、thread_versions、完整的归一化 thread_state_sources 和 completed job。
6. 普通 ThreadRepository.publish() 原子改变关联，触发器失效受影响 owner。已知 dirty 或同批失去成员的 Thread 不能仅凭关联模型输出恢复 clean；新 Thread/仅安全追加的已有 Thread可沿用有效状态。移动后原 Thread 即使没有 Pending Episode，也仍有独立 dirty 维护工作。

## 异常与边界

取消只把当前尝试转 pending，保留此前完整页；重启 recover() 恢复 running job，不重复已提交页。模型/JSON/预算失败保留 dirty 和失败原因，显式 repair 可 retry；关联 epoch 改变会重新从当前成员开始，不能发布旧 snapshot。空 Thread 的 empty_archived 有明确审计记录，不凭空生成历史。所有来源关系按 Agent 重验，已移动 Episode 不属于旧 Thread 新版本来源。

schema 10 增加有界自动恢复：同源失败默认 300 秒起指数退避，连续三次失败后停止自动付费尝试；failure_count/retry_after 跨重启保存。普通轮预算耗尽不算模型失败，不增加 failure_count；显式 `/thread repair` 重置尝试机会且复用已提交页。无故障且配置可容纳来源时，Idle 循环自动推进所有页并发布；持续故障保留可重试工作，不谎报收敛。

重建摘要仍有损，宿主校验证明来源集合与版本一致，不能保证真实模型语义完美。单 Episode 超预算时明确失败；不截断为假成功。没有改写 Journal、MemoryFS、Reflection 或 Compaction 进度。

## 阅读与验证

运行 `uv run pytest tests/test_thread_repair.py tests/test_threads.py`；`test_moved_or_detached_episode_is_never_old_thread_repair_evidence` 是最小例子。覆盖 X→Y、No Thread、保留成员/空 owner、分页/取消/恢复、失败 retry、source revision CAS 和 schema 7→8 回滚。该模块只提供一个有界修复步骤，自动 Idle 触发由 OrganizationRuntime 统一装配。
