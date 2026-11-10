# HeyBuddy-Bench v1.0：360 个案例

该版本保留既有 240 个主集/扩展案例，再增加 120 个长序列。每个 Part 共 120 个案例，而不是把消息数当作题数。四道事后诊断探针不计入正式分母，历史版本、重复 smoke 和重复运行也不增加题数。标签均未完成独立人工复核。

| 集合 | Routing | Memory | Proactive | 合计 |
| --- | ---: | ---: | ---: | ---: |
| v0.4 主集 | 50 | 50 | 50 | 150 |
| v0.4 挑战集 | 10 | 10 | 10 | 30 |
| 原短序列 | 20 | 20 | 20 | 60 |
| 新增长序列 | 40 | 40 | 40 | 120 |
| 总计 | 120 | 120 | 120 | 360 |

## 长序列设计

新集为 120 个场景、60 个家族，每个家族两个有条件差异的变体；84 个开发场景、36 个保留场景，家族不跨集合。每个场景包含 20、24、28 或 32 条新用户消息，共 3,120 条；另有 40 个只推进时间的复查步骤。共有 1,480 个固定检查点。

- Routing 以 20 个项目背景覆盖旧候选缺失、项目/面试/出行交错、同领域独立新任务、模糊指代和历史隔离。每条输入使用真实 Router，先前实际绑定影响后续；新建任务的动态 ID 仅在评分阶段通过先前真实结果解析，模型看不到 Gold 或别名。
- Memory 覆盖求职状态、项目阶段、数据库约束、回复偏好、项目作用域、未决定/正式决定、日期、实体所有者、重命名、撤销及授权污染等 20 个家族。每场景五个显式学习点，其间每条用户消息均保存为独立 completed Run，下一学习点消费全部新增 Run，不跳过中间证据。
- Proactive 以 20 组事项覆盖发现、第二个独立机会、已有进展、取消或完成、普通回顾不恢复、新计划重新开启、安静时段、过期、明确勿扰与优先事项。每条用户 Run 都进入实际增量 Scout；最终时间点最多尝试一次 Attention，不能重复询问直到得到有利动作。

中间消息包含一般方法讨论和对当前任务的细化，部分讨论模板跨场景共享，不能将 120 个场景或 3,120 条消息当作独立随机样本。两个变体会改变候选规模、确认与未确认条件、最终状态、取消/完成或紧急事项，不仅仅替换名称。

## 数据和执行语义

`manifest.json` 冻结四个集合的路径、数量与输入/Gold 哈希；`long/inputs.jsonl` 与 `long/gold.jsonl` 分离。输入顶层 `case_id/track/family/split` 仅用于管理，只有 `input` 内按时间到达的数据进入生产服务。`long/manifest.json` 记录长场景数量、家族、消息数和检查点数。

每个长场景有独立的临时 SQLite 与 MemoryFS，内部保留实际状态，场景之间不共享。固定用户脚本逐条披露，助手为固定确认文本或明确声明的合成建议，因此这是模块级长程状态回放，不是模型生成全部回答的端到端长对话。Thread 卡仍由初始数据定义，没有运行 Organizer 重建全库；没有真实渠道或 Sender。

Memory 与 Router 的已知执行失败保留为失败，仍可消费之后的新消息以观察恢复；不重复旧请求、不补入 Gold。Proactive 若生产 Runtime 进入失败状态，后续源仍记录，但不擅自解除失败水位或伪造成功。预算耗尽或调用费用未知则立即停止所有后续模型调用。

## 指标

主汇总在真实调用前声明：每个案例一票，取既有主集/挑战集保存的最后一次最终结果，合并原短序列开发结果、此次短序列保留结果以及全部长序列结果。缺失和错误保留在固定分母，旧结果不因新输出改变。

长序列单独报告：

1. 整场景通过率：全部预设检查通过、完整回放且无执行失败。
2. 最终检查通过率：最后状态或动作符合要求，允许看到中间失败后恢复的情况。
3. 全部检查点通过率及关键检查点通过数；Routing 另记有效候选召回。

这些是词面/结构化状态断言。Memory 检查预声明的新值及指定旧原文移除，仍可能误罚有历史限定的保留或漏掉改写后的错误；不是 Fact F1 或完整 Faithfulness。Proactive 的静默条件、可接受动作来自合成 Gold，不能当作真实用户偏好。

## 复现和费用

```powershell
uv run python evals/heybuddy_bench/v1_0/build.py
uv run python -m personal_agent.evaluation.heybuddy_bench.longitudinal --validate
uv run python -m personal_agent.evaluation.heybuddy_bench.longitudinal --stage smoke
```

默认无 API、无 `.env` 加载。真实调用只允许显式 `--live`，且在全新阶段目录执行；当前使用项目可信配置中的 DeepSeek Flash，不显示凭证，不打开真实聊天数据库。开发与保留阶段共享冻结代码/协议，不根据开发或保留分数调参。

```powershell
uv run python -m personal_agent.evaluation.heybuddy_bench.longitudinal --stage smoke --live
uv run python -m personal_agent.evaluation.heybuddy_bench.longitudinal --stage dev --live
uv run python -m personal_agent.evaluation.heybuddy_bench.longitudinal --stage reserved --live
uv run python -m personal_agent.evaluation.heybuddy_bench.longitudinal --stage short-reserved --live
uv run python evals/heybuddy_bench/v1_0/report.py
```

复跑需要新的明确授权及评测会话，不能删除账本重新获得费用额度。本轮沿用用户授权的累计 ¥20，承接此前短序列的 ¥0.427512 上界估计，**不是另开 ¥20**。`long-session.json` 与旧 `session.json` 共用会话锁，首次承接核对冻结的旧账本；每次模型请求先持久预留费用，未知 usage 冻结账本。输入按 UTF-8 字节加协议余量预留；按官方高峰缓存未命中输入 ¥2/百万、输出 ¥8/百万估算，实际账单可能因缓存折扣更低。SDK 不自动重试，reasoning 已包含在输出中。

真实请求、响应、逐步文件和 SQLite 仅保存在 `.heybuddy/evaluations/sequence_20261110/`。`report.py` 只读取保存结果、导出无正文统计，不调用模型；输出 [RESULTS](RESULTS.md) 和 `results/summary.json`。原始 150 主集、30 挑战题及 42 个短序列开发场景沿用此前结果，本轮实际新测的是 120 个长序列与 18 个短序列保留场景，不能声称本轮重新运行了全部 360 个案例。

## 本轮一次未知用量的明确批准核对

开发阶段第 113 个请求超时且无 usage，原预算立即冻结。用户于 2026-11-10 明确批准：按该请求完整预留 ¥0.05483 永久占用额度，累计从 ¥1.07227 起算；保留原失败，不重试，只继续未执行场景，总限额仍是 ¥20。

受控入口 `uv run python evals/heybuddy_bench/v1_0/reconcile.py --live --approve-reservation` 仅核对这一条冻结提案。`ReservedUsageBudget` 保留实际已知 Provider token 总数，未知请求的输入/输出上界及费用单独记录；公开 `usage_complete` 仍为 false，只有账务已保守覆盖时 `accounting_complete` 为 true。后续每次预留仍受原总上限约束，新请求若再未知则再次停止，不能自动获得额度。

原账本、失败记录和 `long-freeze.json` 不删除；核对回执另存。后续阶段写入 `*-reconciled` 目录，开发阶段合并原 12 个已尝试场景与尚未执行的 72 个场景，不重跑中断场景。模型、Prompt、Gold 和评分器保持原冻结版本；核对工具及回执哈希另外记录。汇总优先读取这组有回执的完整阶段结果，同时报告完整回放数量和实际用户消息数，不将中断后未消费的消息称为已执行。
