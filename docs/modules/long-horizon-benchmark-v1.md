# Thread Routing Benchmark v1 草案

## 职责与组成

在 [Phase 1 基础设施](long-horizon-evaluation.md) 上增加更长的合成场景、按场景划分的开发集/Holdout、澄清与证据关系指标，以及离线预算预检。生产 Router、Prompt、Agent Loop、Memory Learning 和 SQLite schema 均不修改。

数据入口为 `evals/long_horizon_v1/`，12 场景/216 Turn，开发集 8 场景/144 Turn、Holdout 4 场景/72 Turn，全部有明确合成来源。Gold 是 Agent 显式编写的待人工复核草案；标签在首次规则评分前冻结，不按 Router 输出反标。当前不是已验证的真实模型能力 Benchmark。

`models.BenchmarkProtocol` 保存作者/审阅状态、评分版本、场景长度和划分约束；`dataset.Dataset` 校验 protocol 与数据一致，`load()` 校验其独立 Hash。原 Phase 1 没有 protocol 时保持兼容。报告 Bundle 保存 protocol，重算不用访问当前语料文件；公开报告包含 annotation_status、协议 Hash、评分版本和评测代码 Hash。

`metrics.py` 提供版本 `routing-relations-v1` 的加法指标，原 Pairwise 公式与零分母处理不变。`preflight.py::BudgetProbe/preflight()` 仅用固定选择探测生产 Router 的模型分支与估算输入量。CLI `--preflight` 与 Live/list/report 互斥，始终不加载模型配置或 Key。

## 实际执行路径

1. CLI `--dataset evals/long_horizon_v1` → `dataset.load()` 读取相互分离的 Corpus、Gold、Split、Config、Protocol，验证 manifest，再校验每个 Turn/证据与长度/划分约束。
2. `--variant rule --split dev` → `report.evaluate()`，仍按 begin_run → 真实 Router → 冻结回复 → finish_run 重放。Runner 只接收 Scenario，不接收协议/Gold。来源、数据库和 Memory 隔离保持不变。
3. `metrics.score()` 接收完整预测与独立 Gold，分别计算 Pairwise、澄清和显式历史关系；`aggregate()` 汇总各 Scenario 的 micro/macro 和有效 support。无效/不完整 Replay 不能报告成功，预测错误本身不会导致执行失败。
4. `--preflight --split dev` → `preflight()` 依次使用 continue/clarify/new 模拟 Provider，每个路径/Scenario 独立环境。真实 Router 自行决定何时请求 Provider；Probe 只记录 estimate_request 和输出上限，返回固定动作，无工具、不执行历史工具。
5. CLI 保存 `preflight.json` 和状态文件，只含路径计数、估算、Hash 与代码/Git 元数据，不保存模拟“能力分数”。每请求最多一次模型选择，首 Turn 无候选，故本数据单次开发集最大 8×17=136 请求；真实模型可能产生不同路径。
6. 规则结果照旧支持 `--report` 无模型重算。评分新增字段带版本；旧 smoke 的原始归档汇总不改写，用户主动重算旧 Trace 时会得到当前版本的新字段，不能把两个评分版本静默混合比较。

## 指标与边界

- `clarification.required_recall/miss_rate`：仅 acceptable_actions 恰为 clarify 的 Turn 是必需澄清样本，统计检出或遗漏。含多个允许动作且允许 clarify 的可选项不强行算遗漏。
- `clarification.unnecessary_rate`：不允许 clarify 的 Gold Turn 中预测标记歧义的比例；`precision` 为所有预测澄清中允许澄清的比例。判断的是 Router 的歧义标记，不是冻结助手回复是否包含问句。
- `routing_coverage`：无歧义 Gold 中实际没有弃权的比例。预测弃权仍从 Pairwise 排除，因此必须连同 coverage 报告，不能靠全部澄清获取虚高分。
- `reference_relation_accuracy`：按冻结的 relevant_source_turn_ids 核对过去证据与当前预测 Thread 的同/异关系，不对 UUID 作未来全局最优对齐，不把 Gold 注入 Router。每个有效关系作为一个 support；一个 Turn 只要任一有效关系错误，就记为一次关系失败。
- `error_propagation`：统计相邻且可评价的关系失败中，失败后仍失败的比例、最长连续失败段和长度至少 2 的错误段数。无证据、歧义或弃权 Turn 打断连续段。它描述错误持续的观测关联，不声称后一个错误由前一个因果导致，也不把全部 Pairwise 错误计数冒充“传播率”。

所有比例带 numerator/support；零 support 返回 null。Macro 只平均有效场景并记录数量；Micro 累加分子分母。动作准确率仍为 null，不从不等价 reason 强行推导。评测不涉及 Memory Apply、生成式回答质量、模型主观评审或生产用户隐私数据。

协议与 Gold 语义没有第三方复核，场景与冻结回复的自然性仍有限。Holdout 仅按场景隔离，当前没有执行路由或评分；这是本地可读的内部 Holdout，不是盲测。人工修正必须带版本和原因，不能看模型结果后改标签而沿用同一冻结版本。

## 本轮离线观察与预算建议

开发集规则基线共 144 Turn，执行完整。Pairwise micro F1 约 0.6142，macro F1 约 0.6042；必需澄清遗漏 7/7，过度澄清 1/137，显式历史关系正确 200/335。最长连续关系失败段为 16 Turn。上述结果是 Offline Contract Test 与规则表现，不能当作真实模型评测，也不据此调整标签/Prompt。

三个固定模拟路径的模型分支次数为 continue 111、clarify 111、new 63；单次估算输入最高 2782 tokens。它们只有预算用途，不能看作真实模型调用次数或能力。以每请求估算输入上限 4096、输出 1024 规划，开发集单次最多 136 请求、696320 预留 tokens；Holdout 单次解析上限 68 请求、348160 tokens，不需要执行 Holdout 得到此上限。

若沿用上一轮已核实的未缓存输入 2 元/百万、输出 8 元/百万作为暂定计价假设，开发集单次上界估算为 `(136×4096×2 + 136×1024×8)/1000000 = 2.228224` 元；建议下一轮先做开发集一次、最高 3 元的独立实验。此处只是提案，执行前需复核端点价格并授权。两个 Split 各重复 3 次的同条件上界约 10.027008 元，不应宣称必然低于上一轮 10 元 smoke 预算。

输入估算不是 tokenizer。固定模拟路径不能覆盖所有模型选择；实际输入超规划上限应停止而不是自动提额。当前 LiveConfig 的 5 次/20000 tokens smoke 限制继续保留，完整正式实验的预算配置需要下一步实现并显式授权；本轮没有付费请求。Holdout 不随开发迭代反复跑。

## 阅读与验证

```powershell
uv run python -m personal_agent.evaluation.long_horizon --dataset evals/long_horizon_v1 --list
uv run python -m personal_agent.evaluation.long_horizon --dataset evals/long_horizon_v1 --split dev --variant rule --output .heybuddy/evaluations/v1-rule
uv run python -m personal_agent.evaluation.long_horizon --report .heybuddy/evaluations/v1-rule
uv run python -m personal_agent.evaluation.long_horizon --dataset evals/long_horizon_v1 --split dev --preflight --output .heybuddy/evaluations/v1-budget
uv run pytest tests/test_long_horizon.py tests/test_long_horizon_live.py tests/test_long_horizon_v1.py
```

`tests/test_long_horizon_v1.py` 覆盖 12 场景/时间与引用、8/4 划分、协议冻结与标注状态、遗漏/过度澄清、可选澄清/零分母、连续关系失败、无 Gold 的真实 Router 预算路径和带协议的报告重算。Holdout 只做静态数据校验，不输出 Holdout 预测或评分。沿用网络禁用保护，不读真实 `.env` 或调用模型。
