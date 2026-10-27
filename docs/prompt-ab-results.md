# V0.7.2 Prompt A/B 实测结果

Phase 1 在提交 `7ac3b6e1203cf58a1871ed8e16714e96bce912c8` 验证完成后冻结。Phase 2 使用同一 deepseek-flash、api.deepseek.com、1048576 context window、4096 普通输出预算、相同 Reflection 配置、相同工具实现/权限和合成数据。模型可能由服务商更新；该名称不是不可变模型版本。没有真实私人历史、没有真实 Apply。固定语料/评分哈希为 `b32e193d9902dc24b2584155592a3e36e13f31e0c1e599767269c176a24b1554`。

先运行 12 个开发 Case 的 Phase 1 基线，每个重复两次，再按明确假设隔离比较 Retrieval、Worthiness、Synthesis 三组，其他 Prompt 保持基线。人工复核后先记录 retention.json，最后使用 4 个未用于调参的 Holdout Case 比较基线与最终组合，每个重复两次。完整匿名数值在 [summary.json](../evals/results/summary.json)，所有 Prompt 原文在 [快照目录](../evals/prompts/)，审计见 [Prompt Audit](prompt-audit.md)。

## 配对结果

| 同 Case 对照组 | Baseline | Candidate | API requests | Case 耗时总和（秒） | 估算 USD |
| --- | --- | --- | --- | --- | --- |
| Retrieval：Main + tools，7 Case × 2 | 11/14 | 14/14 | 54 → 44 | 116.527 → 77.639 | 0.0484662 → 0.0326937 |
| Worthiness：重复知识，1 Case × 2 | 2/2 | 2/2 | 11 → 9 | 31.956 → 17.096 | 0.0118605 → 0.0088818 |
| Synthesis：Reflection + Summary，4 Case × 2 | 6/8 | 8/8 | 48 → 50 | 125.835 → 124.639 | 0.0464577 → 0.0502518 |
| 最终组合 Holdout：4 Case × 2 | 7/8 | 7/8 | 34 → 33 | 113.894 → 103.823 | 0.0328977 → 0.0301062 |

耗时为每个 Case 的实际耗时相加，包含正常回答和自动检查，不是两个并发 Case 的墙钟时间；受网络、服务商负载和输出随机性影响。采集没有显式固定服务商 seed/temperature，所有组使用同一接口默认值。分组后顺序运行，未随机交错，因此时延/缓存差异不能全归因于 Prompt。基线整个开发集 19/24；最终组合没有重跑整个开发集，不将隔离候选分数相加冒充最终组合表现。

| 配对组 | Input tokens | Output tokens（含 reasoning） |
| --- | --- | --- |
| Retrieval | 117886 → 82419 | 10917 → 6640 |
| Worthiness | 23979 → 22882 | 3889 → 1681 |
| Synthesis | 94579 → 104534 | 15070 → 15743 |
| Holdout | 54835 → 49454 | 13706 → 12725 |

总计 64 次场景运行、283 次 API 请求，已知 input 550568、output 80371（其中 reasoning 44904），所有采集均有 usage。按 DeepSeek 峰值 cache-miss input $0.3/M、output $1.2/M 估算合计 **$0.2616156**，不是实际账单；缓存/时段价格会影响账单，reasoning 不重复计价。[官方价格来源](https://api-docs.deepseek.com/quick_start/pricing/)。请求前保守预留 4068525 tokens、$2.9605383，均未超过预设总预算 320 requests/6M reserved tokens/$5。预留额度包含未用完的输出空间，不能当实际用量。

## 保留与撤回

保留 Main + Tool Descriptions：明确一般问答/假设直接答；项目沿 Core 根索引到目录/主题；Recall 查历史证据；root directory 使用空串；缺失事实承认无记录；失败/部分发布不盲目重试。开发集中两个假设场景不再查无关工具，缺失记忆的一次失败变为承认无记录。正常项目主题和跨会话检索 4/4 均满足目标读取，历史证据检索 2/2，失败工具 2/2 均只调用一次；这些场景没有退化，但样本很小。候选仍出现过一次非法根路径调用，不能声称已消灭无效工具调用。

保留 Worthiness：强调新增的用户确认长期信息；重复已知内容可直接 false，信息不足时留给 Reflection 比较。重复知识两次都保持 no-op，自动记忆请求从 5 降至 2，自动部分耗时 19.947 → 3.055 秒、估算 $0.005895 → $0.0015222。只测了一个重复场景两遍，不能据此宣称广泛 Precision 提升。

撤回 Reflection 候选：两个偏好词面检查从失败变成功，基线用了“设计动机”而未命中固定“为什么/理由”词，因此该提升不能证明理解改善。两版都曾扩写未确认的“替代方案/权衡”等偏好。只看三个 Reflection 阶段，输出 6655 → 7641 tokens、估算 $0.0147156 → $0.015783，费用上升约 7.2%。没有足够语义收益，保留原版 BASE/EXTRACT/SELECT/CONSOLIDATE。

撤回 Compaction 候选：两版长上下文 2/2 都保留最新方案 B、失败状态、release-check 开放任务。候选 Summary 输出 2134 → 1511，阶段耗时 12.722 → 9.395 秒，但只有两次、无明确质量改善；不凭单次速度选择新 Prompt，保留原版 SUMMARY_PROMPT。

## Holdout 回归与安全

最终组合仍为 7/8：虚构位置场景的多余工具调用消失，但 preference_repro 的第二遍发生 `invalid_create`。Worthiness/extraction 正确识别确认偏好，未修改的 Consolidation 把已存在 human.md 误选为 create；Host 拒绝，未创建计划/写文件，普通答案仍 completed。它没有被解释成 Worthiness 漏判，也没有用 Holdout 调规则或改评分。需要后续更多真实使用数据验证 Reflection 稳定性，本轮保留失败证据。

Holdout 的普通与自动消耗也有不同方向：总请求 34 → 33，但自动检查仍为 20 → 20 次，其 output 6782 → 8697、阶段耗时 53.958 → 63.405 秒，估算 $0.0153882 → $0.0172431。总成本下降不意味着自动部分更省；整体质量提升未获证实。

全部 64 次运行的 Markdown 不变、完整 Journal 保留、无越权写工具尝试检查通过。所有预期阴性场景均没有生成 create/update 提案；其中一次基线长假设回答的原始 Run（含 Provider JSON）超过 16KiB 自动源页上限，检查失败且未推进，没有伪称阴性成功。程序检查通过不等于所有提案语义准确；偏好扩写正是需要人工审阅的例子。

另一个指标限制：合并原文与准确引文能证明来源存在，不能证明整段提案都被来源蕴含。因此不报告一个虚假的“Reflection 语义精确率”。自动指标分别提供 bound/target/action/evidence、无意义反思触发、无写入和历史完整性；需要更多人工语义审阅才能量化泛化质量。

## 可复现证据与限制

manifest 保留原收集器哈希；收集后只纠正报告中 Compaction 被计为 automation_requests 的标签，所有汇总从原始 trace stage 重算，不改变评分或模型运行。原始合成 SQLite/正文 trace 在本机 `.heybuddy/evaluations/`，不会入 Git；公开汇总通过 reporting 校验配对与哈希。Prompt Diff 可直接比较 phase1-baseline.json 和 retained-candidate.json；完整候选和被撤回组也保留，避免只展示成功尝试。

单模型、每例两次、稀疏词面 Rubric、不交错采样和服务商缓存/负载均限制结论。没有证明 Reflection 或 Compaction 质量提高，没有实现后台学习、Scheduler 或 ToolCallReviewer。后续方向是先积累 CLI 使用反馈与人工复核语义集，再决定是否需要更广覆盖的 Prompt 实验；本轮不继续扩展基础设施。


## 指标口径与最终工程验证

零工具期望场景中，“出现至少一次多余工具”的 Run 比例：检索组 2/4 → 0/4，Holdout 1/4 → 0/4。这不是所有类型工具的通用无关调用率。检索组预期阴性内容的 create/update 提案率均 0/12，Holdout 均 0/4；该指标仅衡量错误写入提案，不把失败检查误当成成功阴性。检索组目标主题读取准确率均 4/4。Synthesis 的 Action/target 结构精度均 6/6，但语义 marker 仅让 Update 合约从 4/6 → 6/6；人工仍发现偏好扩写。Holdout 成功 Update 合约 4/4 → 3/4，已生成提案的 Action/target 为 4/4 → 3/3，后一个分母排除了失败，因此不能用来掩盖失败率。汇总的 semantic_reflection_precision 保持 null，没有足够人工标注给出可信语义精度。

最终代码验证：`uv run pytest -q`（临时目录显式放在仓库 .pytest_cache 下）为 **431 passed、1 skipped**，跳过项是 Windows Symlink 权限限制。Ruff check/format、`uv build`、CLI/evaluation help 和场景清单通过。最终 wheel 两个独立进程完成启动、新会话、重启、automation status，schema=5、两个会话持久保留、零模型 Run；Phase 1 checkpoint 已通过三个独立进程 Fake Provider 的普通回答→负判断、稳定偏好→自动提案、重启拒绝流程。所有 pytest 仍为离线，真实 A/B 单独执行，没有真实 Apply。
