# HeyBuddy 三能力 Benchmark

## 职责与架构组成

`evaluation/heybuddy_bench/` 为三个决策点提供隔离运行器，直接调用 Router、Learning、Proactive 的生产实现。数据在 `evals/heybuddy_bench/`：`inputs.jsonl` 是输入，`gold.jsonl` 是隔离标签，`manifest.json` 固定哈希。`dataset.py` 校验；`adapters.py` 建立临时状态并调用生产模块；`baselines.py` 提供三个简单确定性对照；`runner.py` 控制串行执行、临时目录、调用预算与费用；`scoring.py` 计算动作/目标指标；`__main__.py` 提供显式离线或 live 入口。

每题是独立快照，输入里已经包含必要历史与候选状态。数据中的 `episode` 不表示后台 Organizer 已运行。Memory 使用原生 `learn(run_ids=...)`；Proactive 使用原生决策分支，上游 Scout 提取效果不在这个 Track 内。当前默认库 Router 仍关闭模型选择，Benchmark 显式开启一次模型辅助选择，弱规则仍按生产行为优先。

## 代码实际运行路径

1. `python -m personal_agent.evaluation.heybuddy_bench` 进入 `__main__.main()`。`--validate` 调用 `dataset.validate()` 核对 150 个输入/标签、引用、时序和清单哈希后退出。正常执行只通过 `load_inputs()` 读取输入，按类别轮流选取固定子集，不按 Gold 抽样。
2. `runner.run_cases()` 为每道题创建独立 `TemporaryDirectory`。`adapters.evaluate()` 创建 schema 24 SQLite 与 `SessionManager`，不调用 `runtime.open_application()`、不加载应用路径或 `.env`、不连接渠道。临时目录随 case 结束清理。
3. Routing 的 `adapters.routing()` 由输入建立 `Thread` 快照、原始历史与来源绑定，通过 `seed_run()` 写入合法 completed Runs。快照 active selection 不是新 Host 命令，避免 `selection_pending()` 绕过 Router。当前 Run 在 running 状态调用真实 `ThreadRouter.route()`，通过真实 `RoutingRepository.bind()` 保存归属，再根据 reason/ambiguity/实际 Thread 归一化动作。
4. Learning 的 `adapters.learning()` 初始化临时 `MemoryFS`，通过原 MemoryFS/index API 写入初始 Core 与主题，补齐旧数据缺失的中性 Frontmatter。`seed_run()` 保留来源时间和 user/assistant 角色，合法结束。`MemoryLearningService.learn(run_ids=...)` 经过生产 Extraction、Targets、逐文件 FileDecision、证据/唯一片段/hash/隐私校验、原子文件发布与 SQLite 回执。Runner 读取真实前后文件差异，不制造 `facts` 回答，不批准手动 Reflection 计划。
5. Proactive 的 `adapters.proactive()` 用输入证据建立 Journal 来源，初始化 Repository 水位，再载入已有候选快照。`ProactiveRuntime.run()` 执行 Gate → 到期候选 → `AttentionEngine.evaluate()` → Activity/Revision/CAS 复核 → Shadow 审计。已完成、取消、covered、安静窗口、无新来源的已通知事项可以在模型调用前沉默；不会把这些情况当作模型语义成绩。Composer 关闭，`outbound=None`，不构造 Sender 或 Scheduler。
6. 所有模型调用经 `runner.Meter.generate()`，要求空工具列表，调用前使用已有 `evaluation.runner.Budget.reserve()` 预留请求/估算 Token/费用，记录 Prompt 哈希和结构化可见决定。原生子模块仍执行自身输入、输出、超时与来源校验；Provider 仅由 live 入口显式构造，SDK `max_retries=0`。
7. 每道题完成后立即追加并 flush `predictions.jsonl`。全部推理结束、Provider 关闭后，非 Smoke 模式才读取 Gold 并运行 `scoring.aggregate()`，输出 `report.json`。Smoke 不生成能力分数。缺失/重复预测、非法动作、执行错误不会被筛掉；未完成或失败退出码为非零。

## 异常与边界

`ObservedLearning.propose()` 只旁观原 `MemoryLearningService.propose()` 已校验的 PlanItem，记录当前题的最终 Host 决定；`learning()` 只读取本题调用片段，避免复用前题的 FileDecision。实际零变化的等值更新记为 no_op，失败仍为 __error__。旧 v0.2 真实记录的报告污染可由 `analysis.repair_observation()` 根据本题调用和已保存的真实文件效果重算，保留原始记录，不重跑 API、不改变模型输出或 Gold。

显式真实诊断入口 `python -m personal_agent.evaluation.heybuddy_bench.experiment` 使用冻结的 `live_protocol.json` 切分 104 个开发题与 46 个按用户/来源组留出的题。该入口通过原 `load_settings()` 取指定项目配置，只使用模型、端点和 Key，不读取真实数据库/MemoryFS；不展开配置内容。它要求配置的模型与端点匹配协议，使用 JSON 与 reasoning_effort=none、SDK 无重试，所有阶段共用 `SessionBudget` 的持久 CNY 账本。每次分发前记录 pending 和本次输入字节上界/输出上限；已知 usage 到达后按高峰未缓存单价记费并释放未用预留。未确认 usage 或遗留 pending 阻止下一次真实调用。原始输入/输出留在被忽略的评测目录，仅公开无正文汇总。`analysis --candidate-tag final` 区分最终轮，最终轮复用留出组，不能当作全新盲测。生产代码冻结哈希在调用前核验。

原始 v0.2 分数与预先声明的复核分并列：复核分允许合法 Deferred 目录中的新文件名、silent 无选择、六个可合理存入 Core 或现有项目的方案。它不放宽文件正文质量，不把这类评分修正当作生产改进。开发题与留出题仍是 Agent 编写数据，不能称为第三方盲测。

缺失真实模型、Key 环境或价格时 live 入口在调用前拒绝；预算不足、Provider 失败或无效 JSON 不重试。实际 usage 缺失时成本与 Token 保留 null，并停止后续真实请求；已知 reasoning 已在 output 中，不重复收费。只保存安全错误码/异常类型，不打印 SDK 异常正文、私有 Provider payload、隐藏推理或密钥。费用预留使用估算，不是精确 Tokenizer 或外部账单硬保证。

MemoryFS 保留原隐私、hash、路径、索引、局部写入及跨存储回执边界，失败不报告虚假撤销。临时目录必须位于允许写入且满足隐私检查的位置，默认 `.cache` 已被 Git 忽略。用户不能通过 Benchmark 参数选择真实应用数据库或真实 Memory Root。输出目录已存在时拒绝覆盖。

`attention_context` 与 `due_at` 不属于原生 Attention 输入，适配器不添加特殊 Prompt；忙碌的合成用户证据直接保存在 Journal，候选的来源引文随生产对象提供。`last_notified_at` 只投影为候选快照的已评估状态，不能当作真实消息回执。新的候选生成、Scout 覆盖提取、长程问答和文案质量不在这组决策快照成绩里。

新建 Memory 文件名不唯一，只比较合法主题区域；已有目标路径严格比较。no-write 行为与原始动作分分别报告，`content_checks` 仅为词面诊断，不是语义记忆 F1。数据经过 Agent 审阅，尚未独立人工审核，同模板题有关联；公开裁剪题不能报告为原始 Benchmark 官方成绩。

## 阅读与验证

最小离线示例：

```powershell
uv run python -m personal_agent.evaluation.heybuddy_bench --validate
uv run pytest tests/test_heybuddy_bench.py
uv run python -m personal_agent.evaluation.heybuddy_bench --mode smoke --limit-per-track 3 --output .cache/bench-smoke
```

`tests/test_heybuddy_bench.py` 覆盖冻结数据/公开来源、嵌套 Gold 泄漏拒绝、缺题分母、重复预测、输入元数据隔离、临时状态清理、真实 Learning 写入和回执、安静窗口无模型调用、无效输出、预算耗尽、usage 缺失与推理计费。原有 `tests/test_thread_routing.py`、`tests/test_learning.py`、`tests/test_proactive.py` 继续验证生产安全契约。

真实运行的参数和已运行结果见 [Benchmark README](../../evals/heybuddy_bench/README.md) 与 [Review + Evaluation Report](../../evals/heybuddy_bench/BENCHMARK_REPORT.md)。
