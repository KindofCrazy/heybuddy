# Working Context Retrieval

## 职责与架构组成

`retrieval.py::ContextRetriever` 负责相关性和有界来源选择；`core/retrieval.py` 定义独立 ContextRetrieval Protocol、RetrievalResult、RetrievalItem，不导入 SQLite。`ContextBuilder` 决定去重、最终预算及注入，不让检索器更改 System、权限或模型窗口。宿主 `_run_session()` 在 SessionManager.retriever 上绑定 ThreadRepository；切换内部历史流时继续复用同一 Agent 的检索器。

## 真实执行路径

1. Agent.run() → ContextBuilder.begin() 创建原始 Run/用户消息，清空本轮检索缓存。
2. 每轮 prepare() 有界读取 Summary/未压缩完整 Runs；首轮 `_retrieve()` 把当前输入及已加载/当前 Run ID 交给 ContextRetriever.retrieve()，本 Run 后续工具 round 不重复检索。
3. 没有明确历史/延续标记直接返回 fast，不查询 Episode/Thread、不增加模型请求。有历史标记才提取最多 8 个中英文关键词。第一版不用额外 LLM Retrieval Decision；模糊请求交主 Agent 的 Recall。
4. 在短事务和 SQLite VM 预算内，用参数化 instr 匹配当前 Agent 的 Thread 标题，只取一个候选。`_candidates()` 合并该 Thread 最近至多三个 Episode，以及最近默认 200 个未关联 Episode 中的词法候选；没命中 Thread 时则在最近 200 个全部 Episode 中找。Title 命中权重 3、Summary 权重 1，按相关性/来源时间排序，去重后仍最多 episode_limit 条。最多只反序列化有界两组候选，不把整个扫描集载入 Python。
5. Thread dirty 时省略旧 state。若选中更晚的未关联证据，以 newer_unlinked_evidence 提醒旧 state 可能滞后。Episode 标记实际 thread_id/retrieved_via 与来源时间，命中不写归属，不等待 Organizer，不增加 LLM。没有词法证据则 recall_fallback，Recall Tools 保持可用；不声称能理解所有隐含同义表达。
6. 每个 Episode 优先查询末尾最多两个 Run 引用，总计最多两个 raw Runs。重新 JOIN Run/Conversation 验证 Agent、stream 和 completed，核对 payload 字节/数量后才反序列化，单 Run 最大 65536 字节；不使用 Summary 伪装原始来源。原始 ToolCall/ToolResult 和 Provider 数据完整保留，旧调用不执行。
7. 返回有时间、ID、revision、sequence、source_hash 的引用及完整消息；raw 按真实时间排序。`_select_history()` 对比当前/近期/其他已选 Run ID 去重，调用 ID 碰撞时整条旧 Run 省略。历史 marker 是低信任 user JSON，原消息仅重放证据，不写 Journal。
8. Base/Core/时间/Working Summary/近期完整 Runs/当前 Run/工具定义先占预算。可选检索最多 min(12288, input_limit/3, 触发水位剩余) 估算 tokens，按 Thread → Episode → raw Runs 逐单位选取；不能容纳的旧 Run 整条省略。不会仅为可选材料触发额外 Compaction。最后原 prepare() 再完整预算检查；当前/受保护近期 Run 太大仍明确失败。
9. 原始历史插在近期/当前消息之前；新用户消息最后，marker 提醒按来源时间、最新明确更正和不确定性判断。历史命令不授予权限，工具 Registry 仍过滤和执行前复查。检索错误仅返回 unavailable，正常历史/Core/存储错误仍按原路径处理。

## 异常与能力边界

以上 3 Episode、2 raw Run、65536 字节、12288 tokens、200 个最近 Episode 均为 RetrievalPolicy 默认值；宿主 Settings 的 AGENT_RETRIEVAL_* 可在有界范围内调小/调整，详见 [配置](config.md)。

词法相关性不等于语义检索，关键词可能漏掉同义词或命中宽泛话题，且当前不做模型消歧。Thread 候选标题 SQL 有 VM 预算；补充候选默认只扫描最近 200 条，较旧内容由 Recall 主动搜索。dirty Thread 由 Idle 修复路径恢复，完成前省略旧 state；最新未关联 Episode 也可立即检索。摘要/提示词不能保证真实模型正确区分建议、假设和决定，需要独立 live 评测。

没有新长期 Memory 副本。Working Summary 继续使用原 context_summaries/checksum/事务边界，只调整为保留影响当前对话的目标、约束、进展和待续信息，不承担几个月的生平总结；旧检查点不删除、不重建。

此 Summary Prompt 变化是 P1 明确要求的职责调整，记录在 `evals/prompts/p1-working-context.json`，尚未做真实模型收益验证。Phase 1、retained-candidate 和 retention 记录原样保留，Main/Tools/Worthiness/Reflection 仍与留存实测版本相同；不改 ground truth/scorer，不把原评测成绩挪用于 P1。

## 阅读与验证

启动后直接说“继续昨天的 HeyBuddy 架构讨论”；`/context status` 查看 retrieval_path、retrieved_items、预算与 Compaction 状态。最小离线例子在 `tests/test_working_context.py::test_cross_day_thread_episodes_raw_runs_times_and_current_input_once`。测试还覆盖 fast、无 Episode、无 Thread、Recall fallback、旧 state、去重、完整工具/Provider 重放、大 Run、可选检索预算、跨 Agent 和历史授权注入。运行 `uv run pytest tests/test_working_context.py tests/test_compaction.py`。

Review Fix 的 `tests/test_review_timing_retrieval.py` 新增长耗时工具开始时间差、跨午夜/短时间换话题/跨天、provisional 追加屏障；并直接验证“继续之前 HeyBuddy 的 Memory 设计”和没有显式 Thread 名称的“自动整理记忆机制最后决定”问题，确认最新未关联 Episode 可检索、原归属不变、预算与 Raw Run 去重继续有效。
