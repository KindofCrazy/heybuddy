# Context Compaction：完整 Journal 与有限模型视图

## 职责与架构组成

`src/personal_agent/core/budget.py` 定义窗口和估算；core/context.py::prepare 编排减压；core/replay.py::ReplayView 管当前 Run；core/compaction.py::compact_step 管完整历史前缀。storage/context.py 和 storage/harness.py 保存不同检查点，原 messages 保持完整。

## 机制流程

1. 在发送前估算输入大小，判断是否接近窗口上限，或是否还有未读完的历史。
2. 把过大的工具正文保存为可查产物，输入里保留引用和预览；压力继续增大时，省略已经看过的旧结果正文，保护最近结果。
3. 优先把较早、已经结束的完整交互汇总为会话摘要，保留近期交互。
4. 若历史已无可压部分，再判断当前执行的旧交互是否已完整结束且可安全摘要；保留原请求、近期交互和必要模型协议数据。
5. 重建输入并重新估算。无法缩减且仍超限就停止；模型明确报告超限时只允许一次补救，绝不为压缩重新执行工具。

## 代码实际运行路径

1. prepare 应用 ReplayView.project：超过 16 KiB 的工具 data 存为产物，模型视图使用引用/预览，原 ok/error 与 Journal 保留。tool_output_get 结果不再次卸载。
2. 超默认 80% 水位时做 micro projection：默认保护最近三个工具结果，仅清理已消费旧结果，且不会为清理而卸载不超过 512 字节的正文。成功模型响应后才 mark_consumed。
3. 历史还有页或仍有压力时，compact_through(recent_runs) 选择 completed/failed 完整 Run 前缀，默认保护最近两个 Run。compact_step 用有界原消息发无工具摘要请求，校验后原子提交会话检查点，再重新加载。
4. 无可压历史且仍有压力时才 compact_current：选择已配对旧前缀，保留原用户请求/图片及最近两个 assistant 交互周期，并要求 Provider.safe_compaction 明确支持。
5. 当前 Run 摘要以 run_working_summary user JSON 注入，绑定来源 hash、边界、revision，不推进会话 through_sequence；未来历史摘要仍读原消息。
6. 重新估算，减压目标为输入上限 60%。不能压但低于硬上限可返回，仍超则 context_overflow；步骤耗尽则 compaction_step_limit。
7. 明确 context_length_exceeded 可触发本 Run 一次 reactive prepare + 请求补救，不重跑工具。

## 数据与恢复

| 数据 | 存储/边界 | 用途 |
| --- | --- | --- |
| 原消息 | messages，原 Run/sequence | 审计、证据、历史摘要 |
| 会话摘要 | context_summaries，through_sequence | 跨请求与重启的有界历史 |
| Run 摘要 | Harness run_summary，消息偏移/hash/revision | 当前执行派生视图 |
| 工具产物 | Harness tool_artifacts，来源绑定 ID | 有界分页取回正文 |

产物最多 1 MiB，tool_output_get 校验 Agent/Conversation/source；保存失败或超限时不能声称可完整取回。重启不自动续跑 interrupted Run。

## 异常与边界

输入硬上限为 window-output reserve-safety margin。估算使用 UTF-8 序列化大小，不是 tokenizer；usage 独立记录。空/超长/非法/无收益摘要和 Provider/超时失败不推进检查点；此前成功批保留。提交前检查 deadline 和 task.cancelling，拒绝晚到结果。摘要调用不执行工具、不写 Memory。

OpenAIProvider.safe_compaction 拒绝未知 namespace/item_reference 等无法安全重排的数据。未声明能力的 Provider 不做 Run 内摘要。手动 /compact 仅在无活动 Run 的可信入口压缩历史。budget=None 的嵌入兼容模式不猜窗口、不做摘要，只允许单页且估算不超 262144 的小上下文。

## 阅读与验证

`uv run pytest tests/test_compaction.py tests/test_harness.py` 对照工具配对、Provider 数据、取消、事务、Run 内压缩和补救。/context status 只读统计，不触发模型。历史版本通过数量不代表本轮验证。

## 未来改进

- 按工具类型明确必须保留的证据字段，减少统一清理的语义损失。
- 测量任务完成率、事实保留和多次摘要漂移，不能只看字节节省。
- 分层记录压缩成本与节省量，再用证据调整水位与保留策略。

## Thread 摘要隔离（schema 24）

默认 Harness 通过 SQLiteThreadHistory 使用独立 `thread_context_summaries`，键为 Thread/Conversation/source；`storage.context.page/compact_through/save_summary` 在读取 payload 前过滤 Run 绑定。不同 Thread 的 sequence 可以交错，压缩前缀仅在该 Thread 的完整 Run 序列中连续。CAS、checksum、来源 hash、近期保护与多层压缩规则沿用；不会注入旧 Conversation 混合摘要。可信纠正绑定会事务失效原/新 Thread 在该来源的摘要，之后从原 Journal 重建，不自动调用模型。测试：`tests/test_thread_routing.py`。
