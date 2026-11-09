# HeyBuddy-Bench v0.4

主集仍为三个 Track 各 50 题，共 150 题；119 自建、31 公开改编（TIAGE 5、IRC 3、LongMemEval 8、TriggerBench 15）。四道 MAB 虚构关系保留在旧版诊断集，主集改用个人笔记更新题。另有 30 道挑战题，每 Track 开发/保留各 5 题；场景和跨语言变体有关联，不能当作独立盲测。

旧版输入、Gold 与报告均保留。当前修改来自任务定义审阅、输入/接口核对及真实失败轨迹，仍未独立人工审核。公开题是 HeyBuddy 改编任务，不能报告原 Benchmark 官方成绩，也不能排除预训练接触过公开数据。原 source ID、revision、native_gold 和旧记忆原引文保留在 Gold provenance，不向任务模型提供。

## 数据与评分

Router 保持原生动作与 Thread 归属判断；TIAGE 的话题变化可合理归入日常闲聊或当前闲聊。Memory 支持有证据的多文件变化和声明的 Core/项目存储替代；检查目标、必要新值、指定旧原文移除、其他文件保护及部分跨文件事实。Proactive 对“现在不联系、以后再看”检查实际持久化的未来时间，不能把 defer 到今天冒充遵守今天 DND。

LongMemEval 旧状态改为带主题名、旧事实和普通 Frontmatter 的 MemoryFS，最新用户原话不变；没有把新答案塞进旧记忆。该修订的影响必须使用旧 Agent 重测，不能算作 Agent 提升。字面断言仍不能完整识别否定、范围与所有同义表达；正文语义存疑时进入人工审核，不事后随候选分数改答案。

`cycle.py` 直接调用生产 Router/Learner/Proactive；每题独立临时 SQLite/MemoryFS。Proactive 是给定候选的门控/Attention 测试，含多候选选择，尚未测完整 Scout 发现率。没有 Sender、Composer 或后台应用实例，始终 Shadow。只有任务 `input` 进入适配器，Gold 在推理结束后评分；无效输出保留失败，不删字段或重试。

## 复现

从仓库根目录运行。当前 v0.4 使用专用 cycle/state/fact/temporal scorer；旧通用 CLI/score.py 的动作路径分用于旧版，不能与当前状态断言分混用。

```powershell
uv run python -m personal_agent.evaluation.heybuddy_bench --validate --dataset evals/heybuddy_bench/v0_4
uv run pytest
uv run python -m personal_agent.evaluation.heybuddy_bench.cycle --phase baseline --revision v0_4 --part development --root .cache/bench-v4-baseline
uv run python -m personal_agent.evaluation.heybuddy_bench.cycle --phase baseline --revision v0_4 --part reserved --root .cache/bench-v4-baseline
# 显式真实调用：已配置匹配的模型、端点和 Key，并确认当前价格后执行。
uv run python -m personal_agent.evaluation.heybuddy_bench.cycle --phase candidate --revision v0_4 --part smoke --live --root .heybuddy/evaluations/v4-NEW
uv run python -m personal_agent.evaluation.heybuddy_bench.cycle --phase candidate --revision v0_4 --part development --live --root .heybuddy/evaluations/v4-NEW
# 在最终代码冻结后才评保留部分；不要据其结果反复刷分。
uv run python -m personal_agent.evaluation.heybuddy_bench.cycle --phase candidate --revision v0_4 --part reserved --live --root .heybuddy/evaluations/v4-NEW
# 事后归因探针，单独报告，绝不能并入主集或保留分。
uv run python -m personal_agent.evaluation.heybuddy_bench.cycle --phase diagnostic --revision v0_4 --part diagnostic --live --root .heybuddy/evaluations/v4-NEW
uv run python evals/heybuddy_bench/report_cycle.py --root .heybuddy/evaluations/v4-NEW --output .cache/v4-summary.json
```

真实入口复用可信项目配置加载器，只取已批准的 `api.deepseek.com` / `deepseek-flash` / Key，不打开用户真实数据库或 MemoryFS，不展开或打印配置。SDK 不重试，JSON、关闭思考；所有真实阶段共用持久账本，本轮上限 ¥20，旧 ¥1.875786 另计。没有私有历史账本时使用公开且哈希相同的 `budget_start.json`，不会伪造新调用或增加可用预算。pending 或 usage 不明时停止；费用按已核实的高峰未缓存单价估算，不能冒充外部账单。

输出目录不可覆盖。阶段分别冻结数据与代码；所有候选补丁和生产源码哈希保存在 `../candidate_snapshots/`，源换行归一化哈希方便跨平台核对。旧 Agent 对照使用 `b2a954a` 源码快照，仅替换评测模块与当前数据，配置仍来自原授权入口，不复制密钥或私人数据。模型别名和生成具有时变/随机性，不保证重跑逐题完全一致。

`rescore_cycle.py` 仅允许任务输入完全一致的版本重算，零 API；v0.4 的八个输入变化必须重新推理。完整原始输入、输出、diff、回执留在忽略目录；公开汇总只含决定、指标、错误、Token/费用和 Prompt 哈希。人工队列为本目录及 `../v0_4_challenge/human_review.csv`，所有 reviewer 字段保持未填写，完整上下文请按 case_id 核对 JSONL。

本轮真实结果、剩余评分误判和 Agent 回归见 [迭代报告](../ITERATION_REPORT.md)。`../review_probes/` 的 4 道反事实题是在失败分析后构建的诊断，单独冻结，不属于新的盲测。`../results/cycle_20261108_review.csv` 记录具体争议及后续复核要求，不能将编码 Agent 的判断冒充独立人工审定。
