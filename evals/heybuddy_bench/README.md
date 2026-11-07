# HeyBuddy-Bench v0.2

当前为 **150 个经编码 Agent 审阅的决策快照**：Thread Routing、Memory Learning、Proactive Decision 各 50 题。独立人工审核尚未完成，`human_reviewed=false`。已完成确定性 Baseline 与离线生产链路验证；真实模型 HeyBuddy 成绩尚未运行。

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

真实模型命令使用环境中的 `OPENAI_API_KEY` 或 `AGENT_API_KEY`，不读取 `.env`，不寻找其他凭据。显式提供模型、可选兼容端点、美元每百万 Token 输入/输出单价和预算。下面的模型、价格与端点必须由执行者填写；不提供猜测价格。

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
| `results/` | 本次 Baseline 逐题预测/指标及离线契约验证结果 |

```powershell
uv run python evals/heybuddy_bench/build_dataset.py
uv run python evals/heybuddy_bench/export_review.py
uv run python evals/heybuddy_bench/score.py .cache/bench-baseline/predictions.jsonl
```

测试集不用于训练或调参。同一模板、同一公开对话或同一合成用户下的题有关联；每 Track 50 题的区间仅作描述。公开部分是裁剪与重新标注后的 HeyBuddy 任务，不能宣称为原始五个 Benchmark 的官方分数。
