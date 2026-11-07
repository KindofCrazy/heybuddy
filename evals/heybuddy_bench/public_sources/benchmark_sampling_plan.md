# 已执行的公开抽样与替换方案

2026-11-07 原目标 45 个公开配额经过原文检查后，冻结为 **35 个公开改编题 + 10 个自建替代题**。实际 ID、原始标签、摘录、文件哈希和 revision 全在 `selected_source_records.jsonl`；HeyBuddy 标签及改编理由在 `../gold.jsonl`。

| Track | 最终公开来源 | 公开数 | 替代自建数 | 选择理由 |
|---|---|---:|---:|---|
| Routing | TIAGE 5、IRC 3 | 8 | 2 | 使用可解释的对话/技术支持转向；排除入群与无内容控制事件 |
| Memory | LongMemEval 8、MemoryAgentBench 4 | 12 | 8 | 用户证据更新与四个不同合成知识关系；排除长篇通用 QA、小说预测和重复扩展上下文 |
| Proactive | TriggerBench base_context 15 | 15 | 0 | 时序、状态、条件与注意恢复；保留正负对照并重新考虑 HeyBuddy 安静时段 |

原 `verified_public_candidates.jsonl` 的 10 条只有原生标签，保留作历史候选记录，不能直接评测。IRC `msg_1004` 是入群事件，排除；原 `msg_1006` 是对新 NTFS 问题的助手解释，不作为独立用户切题题，改选 `msg_1005` 的原始问题。未按旧脚本的散列次序强凑 QA 配额；最终采用明确、可复核的固定 ID 清单。

`select_public_cases.py` 仍是历史候选探索工具，不生成当前冻结集。它原先只保存问题/答案、不保留足够证据；跨 MAB 子集的随机 QA 并不保证适合 Memory Learning。当前路径改为 `curate_public.py` 固定原文摘录 → `adapt_public.py` 显式标签 → `build_dataset.py` 校验并冻结，不依赖模型打标。

公开改编并不等于官方 Benchmark 子集原任务成绩。LongMemEval 采用 evidence-only 裁剪而非 S 全历史；MAB 不评多跳 QA；IRC 不评整张回复图；TIAGE 不直接评 topic-shift；TriggerBench 受 HeyBuddy Host 策略影响。完整出处、许可证、缓存重建方法见 [SOURCES.md](SOURCES.md)。独立人工审核队列尚未完成。
