# HeyBuddy-Bench

当前迭代版为 [v0.4](v0_4/README.md)：主集仍为 150 题，增加多文件/跨文件状态断言、未来复查边界和 30 道含开发与保留部分的挑战变体。最新结论见 [本轮 Review + Evaluation Report](ITERATION_REPORT.md) 与 [逐题结果](results/cycle_20261108_summary.json)，方案见 [迭代计划](ITERATION_PLAN.md)。下文保留 v0.2 的数据与首次交付入口；当前版本的测评使用专用 `cycle --revision v0_4`，不能混用旧评分。

当前为 **150 个经编码 Agent 审阅的决策快照**：Thread Routing、Memory Learning、Proactive Decision 各 50 题。独立人工审核尚未完成，`human_reviewed=false`。已完成确定性 Baseline、离线生产链路验证和 DeepSeek 真实对照；模型结果与局限见 [真实测评报告](LIVE_EVALUATION_REPORT.md)。

阅读 [Benchmark Review + Evaluation Report](BENCHMARK_REPORT.md)、[Schema 与评分协议](SCHEMA_AND_TEMPLATES.md)、[公开来源与改编](public_sources/SOURCES.md)。生产调用路径见 [模块说明](../../docs/modules/heybuddy-benchmark.md)。

| Track | 原自建题 | 新增替代题 | 公开改编题 | 总计 |
|---|---:|---:|---:|---:|
| Thread Routing | 40 | 2 | TIAGE 5 + IRC 3 | 50 |
| Memory Learning | 30 | 8 | LongMemEval 8 + MemoryAgentBench 4 | 50 |
| Proactive Decision | 35 | 0 | TriggerBench 15 | 50 |

每题重置 SQLite 与 MemoryFS；不串接独立快照，不启动应用、后台维护 Worker、QQ/NapCat 或 Sender。Proactive 调用真实 `ProactiveRuntime.run()` 的决策分支并关闭 Composer。这里的“完整 HeyBuddy”指决策点之后的生产链路：Memory 包括提取、选文件、整合、来源校验、实际临时文件写入与回执；Proactive 的上游机会提取已由输入快照给定，不是 Scout 发现率或全程主动聊天评测。

## 复现

以下命令均从仓库根目录运行，使用项目已有依赖。首次建议先校验和跑单元测试，再做小规模 Smoke。

```powershell
uv run python -m personal_agent.evaluation.heybuddy_bench --validate
uv run pytest
uv run python -m personal_agent.evaluation.heybuddy_bench --mode smoke --limit-per-track 3 --output .cache/bench-smoke
uv run python -m personal_agent.evaluation.heybuddy_bench --mode baseline --limit-per-track 0 --output .cache/bench-baseline
```

Windows 受限执行环境若不能创建 uv 缓存或临时目录，可将 `UV_CACHE_DIR`、`TEMP`、`TMP` 指向仓库内被忽略的 `.cache` 子目录；asyncio 仍需要系统允许本地 socketpair。不要关闭 `tests/conftest.py` 的外网禁用保护。

通用真实模型入口使用环境中的 `OPENAI_API_KEY` 或 `AGENT_API_KEY`，不读取 `.env`，不寻找其他凭据。显式提供模型、可选兼容端点、美元每百万 Token 输入/输出单价和预算。下面的模型、价格与端点必须由执行者填写；不提供猜测价格。

```powershell
uv run python -m personal_agent.evaluation.heybuddy_bench --mode live --limit-per-track 3 --model YOUR_MODEL --input-price INPUT_USD_PER_M --output-price OUTPUT_USD_PER_M --max-cost 1 --max-requests 30 --max-tokens 1000000 --output .cache/bench-live-smoke
# 全量仅将 --limit-per-track 改为 0，并显式调整请求/Token/费用预算。
```

原生请求的上下文配置为 131072，所选模型必须支持这个配置；Router / Learning / Attention 沿用各自生产输出上限。SDK 重试关闭。费用预留使用项目的 Token 估算，报告按实际 usage 和给定单价估算 API 成本，不能作为账单保证。缺失 usage 时保留 `null` 并停止后续真实请求；推理 Token 已包含在 output 中，不重复计费。未执行真实 API 的 Smoke 成本为零，其 Token usage 不冒充真实使用量。

每次运行需使用新输出目录。`predictions.jsonl` 逐题追加并 flush，含决策、文件变化、回执、安全错误码、请求数、Token 和费用；`report.json` 包含主要指标、来源分组和失败 ID。Smoke 报告只给运行计数和错误，不计算能力分数。存在执行错误或预算导致的未完成题时进程返回非零；评分器将缺题纳入失败分母。

## 数据文件

| 文件 | 用途 |
|---|---|
| `inputs.jsonl` | 当前 150 题；Runner 只将每行 `input` 传入适配器 |
| `gold.jsonl` | HeyBuddy 标签、可接受动作、内容检查和原始公开标签/版本；推理结束后才由 scorer 打开 |
| `manifest.json` | 冻结数量、来源、SHA-256 和独立人工审核状态 |
| `public_sources/selected_source_records.jsonl` | 35 条公开原文摘录与原始标签；**不是模型输入** |
| `public_sources/curate_public.py` | 从本地原始数据缓存提取固定 ID 的摘录，不发模型请求 |
| `public_sources/adapt_public.py` | 显式改编规则和 HeyBuddy 标注 |
| `build_dataset.py` / `assemble_150.py` | 从保留的 105 条种子、公开摘录与 10 道替代题离线重建当前集 |
| `custom_inputs.jsonl` / `custom_gold.jsonl` | 保留的原始 105 条候选，用于追溯修订，不用于当前运行 |
| `build_custom.py` | 历史种子生成器，清单写入 `seed_manifest.json`，不覆盖当前清单 |
| `synthetic_personas.json` / `contrastive_pairs.json` | 原合成背景和三组完成状态正负对照 |
| `human_review.csv` / `export_review.py` | 当前 150 题的独立人工审核队列；不把 Agent 审阅填成人工确认 |
| `validate.py` / `score.py` | 当前数据校验与保存结果重算入口 |
| `results/` | Baseline、离线契约、真实原始/最终逐题指标及第一版候选汇总 |
| `live_protocol.json` | 真实调用前冻结的开发/留出划分、价格、累计预算与次级复核方案 |

```powershell
uv run python evals/heybuddy_bench/build_dataset.py
uv run python evals/heybuddy_bench/export_review.py
uv run python evals/heybuddy_bench/score.py .cache/bench-baseline/predictions.jsonl
```

本次真实诊断按 [冻结协议](live_protocol.json) 将 104 题用于开发、46 题按合成用户/公开来源组留出；仅根据开发错误做生产修复，不训练模型。最终兼容性修复后复用了留出组，不能宣称新的盲测。同一模板、同一公开对话或同一合成用户下的题有关联；每 Track 50 题的区间仅作描述。公开部分是裁剪与重新标注后的 HeyBuddy 任务，不能宣称为原始五个 Benchmark 的官方分数。

## 本次真实对照的复现

专用 `experiment` 入口复用可信 `load_settings()`，只取得显式批准的模型/端点/Key，不打开真实数据库或 MemoryFS。本次协议限定 `api.deepseek.com`、`deepseek-flash`、JSON、关闭思考、累计 10 元。价格使用已核实的高峰未缓存人民币单价；此入口的费用字段为 `cost_cny_upper_estimate`，不要与通用入口的美元字段混用。输出目录必须被 Git 忽略，原始 Prompt/响应仅保存在本地。

下面是实际执行顺序。新实验需使用新目录；`original-*` 应在原始提交 `1378ee6` 的生产模块上执行，`candidate-*`/`final-*` 应使用各自冻结的生产代码。代码、数据、协议哈希记录在本地 provenance 和公开汇总中。模型调用本身非确定性，不能保证逐题重复。

```powershell
$root = '.heybuddy/evaluations/hb-live-NEW'
uv run python -m personal_agent.evaluation.heybuddy_bench.experiment --stage smoke --root $root
uv run python -m personal_agent.evaluation.heybuddy_bench.experiment --stage original-dev --root $root
uv run python -m personal_agent.evaluation.heybuddy_bench.experiment --stage original-holdout --root $root
# 完成开发修复和离线检查后冻结；不要覆盖已经运行的阶段。
uv run python -m personal_agent.evaluation.heybuddy_bench.analysis --root $root --candidate-tag final --freeze-candidate
uv run python -m personal_agent.evaluation.heybuddy_bench.experiment --stage candidate-dev --candidate-tag final --root $root
uv run python -m personal_agent.evaluation.heybuddy_bench.experiment --stage candidate-holdout --candidate-tag final --root $root
uv run python -m personal_agent.evaluation.heybuddy_bench.analysis --root $root --candidate-tag final --output .cache/live-summary.json
```

共享账本在请求前持久化预留，在已知 usage 后结算；请求状态不明或 usage 缺失时停止，不自动重试，也不能删除账本重新获得预算。分析只在推理后读取 Gold，同时输出不变 v0.2 主分和预先审阅的次级宽容分。原始版本 Memory 的跨题观测错误只依据本题已存调用/实际文件效果修正，原始响应保留；这部分不计为模型提升。
