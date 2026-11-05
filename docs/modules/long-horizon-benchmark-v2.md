# Thread Routing Benchmark v2 草案

后续已授权的三个开发场景真实模型入口、共享预算与摘要审阅协议见 [v2 Pilot](long-horizon-pilot-v2.md)。本文保留 interval=3 的离线实验条件；Pilot 使用独立的 interval=6 配置。

## 职责与架构组成

在 [v1](long-horizon-benchmark-v1.md) 上扩展为 30 个合成场景、540 Turn，开发集 21 场景/378 Turn，Holdout 9 场景/162 Turn。保留原 12 场景及其 Gold，原 Holdout 不移入开发集。语料、标签和六个对比例子在首次 v2 评分前冻结；Gold 仍是 Agent 编写、未经独立人工复核的草案，不能称为真实用户数据或独立盲测。

`evaluation/long_horizon/state.py::ProductionMaintenance` 在隔离重放中调用真实 `EpisodeOrganizer`、`ThreadOrganizer`、`ThreadRepository` 的发布及 dirty-state repair；没有另写状态数据库或通过 Gold 修正归属。`ExtractiveContractProvider` 是固定的离线引用提取器，按真实输入给出符合生产结构的输出，验证持久化与候选卡接线，不能代表模型的语义摘要能力。

`policies.py::PolicyProvider` 仅为路由请求追加冻结的任务身份说明和六个独立 few-shot；`ablation.py::evaluate_ablation()` 控制五组增量实验，`RoutingProbe` 返回固定选择并记录请求结构和估算。当前 CLI 的 `--ablation` 只能运行 **Offline Contract Test**，不加载 Settings/Key、不联网、不计算脚本选择的模型能力分数。原有 v1 Live profile 继续限制为 v1，不能用于 v2 或状态维护的付费调用。

## 代码实际运行路径

1. `__main__.main()` → `dataset.load()` 核对 Corpus、Gold、Split、Config、Protocol 与 manifest。`--ablation` 与 Live、preflight、list、report 和 model-assisted Variant 互斥，实验仅接受 dev。每组、每场景独立创建环境。
2. `evaluate_ablation()` 创建 `RoutingAdapter` → 真实 `ThreadRouter`；`PolicyProvider` 包装只记录并返回固定动作的 `RoutingProbe`。原系统 Prompt 与用户消息保留，附加规则和示例只进入路由系统说明；整理请求不使用这些示例。
3. `replay()` 逐 Turn 创建 Run、保存当前用户消息、路由、保存冻结助手回复并完成 Run。初始用户请求和未来 Turn 不混入维护请求。当前 Run 完成后，才 `await ProductionMaintenance.after_completed()`；下一 Turn 在维护返回后才开始。
4. 默认每三条完成的 Run 触发一次维护，以当前合成时间加两秒模拟空闲，策略为 `page_runs=8`、`revision_seconds=1`、`idle_seconds=1`。真实 `EpisodeOrganizer.organize()` 从已完成 Journal 生成 Episode；引用提取器只摘录当时提供的用户原话及时间，不使用冻结助手回复作为用户证据。
5. 有界调用 `ThreadOrganizer.organize()` 消费 Episode，最多四次；调用 `repair(at=...)` 最多 32 次，使用真实来源、固定 Thread 归属、修订和 CAS 更新 current_state。引用摘要保留首个身份锚点和最近三行，最多 1800 字符；历史错误合并仍会进入错误 Thread，不允许用 Gold 悄悄纠正。此调度是受控实验安排，不等于生产 Idle Scheduler 的墙钟行为。
6. 后续真实 Router 从 Repository 读取候选卡，看到已发布的有界 current_state。`ObservedMaintenanceProvider` 分开记录 episode/thread 的估算输入与输出预留，`RoutingProbe` 记录路由的状态卡暴露数、实际 Prompt Hash 与估算输入。
7. `evaluate_ablation()` 保存私有逐步进度、完整 Bundle、状态与 Hash，全部重放完整才生成汇总。`recompute_ablation()` 校验状态、Hash、数据、配置、场景覆盖及 Replay 完整性后无模型重算；公开汇总只包含配置、ID、Hash、计数及估算，不含原话或状态正文。

## 对照实验与异常边界

| 组别 | 状态 | 路由 Prompt | 弱规则 |
| --- | --- | --- | --- |
| baseline | 无 | 原版 | 原版 |
| state | 真实发布路径，固定引用提取 | 原版 | 原版 |
| identity | 同上 | 加稳定任务身份说明 | 原版 |
| fewshot | 同上 | 再加六个独立对比例子 | 原版 |
| deferred | 同上 | 同 fewshot | 换话题与临近续接交模型判断 |

生产 `ThreadRouter` 新增默认关闭的 `defer_weak_rules` 参数，只能与 `model_selection=True` 同时使用；默认 Runtime 没有开启它。实验需要这条最小入口，是因为原规则已经决定的 Turn 不会请求模型，单改 Prompt 对它们没有作用。Host 显式选择、ID、唯一标题等既有处理不变，仍由真实 Router 执行和校验，不复制一份实验 Router。

整理失败、超过有界队列或摘要发布失败会使 Replay 停止，error_stage 为 maintenance，已完成 Turn 保留；取消继续传播，不能冒充成功。状态需要在下一次路由前发布，不能读取未来消息。核心预算在实验中显式配置 131072，整理输入/输出策略为 32768/4096；这些是离线契约参数，不自动覆盖真实模型窗口或付费预算。

当前提取式状态会保留带时间的旧原话与修订原话，不保证结构化解析最新值、矛盾消解、取消/完成状态、摘要遗漏或恢复问答正确性。状态维护输出不是 Gold，也没有语义评分。未来真实摘要实验需另配有来源的阶段预算和语义核验，不能把本轮固定 Provider 的接线通过当成摘要准确率。

## 指标与诊断

`metrics.py` 当前评分器为 `routing-identity-v2`，保持 Pairwise 和 v1 关系指标公式，新增两个指标族，均只统计无 Gold 歧义且预测没有弃权的 Turn：

- `fragmentation`：按 Gold 话题分组，统计一个话题被分配到多少实际 Thread。`mean_extra_assignments` 为额外 Thread 数之和除以话题数；`affected_group_rate` 为被拆成多个 Thread 的话题比例。
- `cross_thread_contamination`：按实际 Thread 分组，统计混入多少 Gold 话题。同样报告平均额外话题数与混入多个话题的 Thread 比例。

平均额外数可能大于 1，不能解读成概率。所有指标保留分子、分母及 micro/macro，空分母为 null，并与 coverage、澄清遗漏/误报、Pairwise Precision/Recall/F1 一起看。仅追求 Recall 会鼓励误合并，全部澄清又会减少可评分样本。原始已归档结果不改写；显式重新评分旧轨迹得到新版本指标，需要同时标明原实验和本次评分来源。

对旧 v1 非思考实验的只读重算显示，规则基线拆分了 17 个 Gold 话题中的 5 个，模型辅助拆分了 14 个；每话题额外 Thread 均值分别为 0.2941 和 1.2353。模型同时减少了误合并，说明需要同时控制拆分与污染。重算没有新增模型请求，也不能证明新 Prompt 有收益。

## 阅读与验证

```powershell
uv run python -m personal_agent.evaluation.long_horizon --dataset evals/long_horizon_v2 --list
uv run python -m personal_agent.evaluation.long_horizon --dataset evals/long_horizon_v2 --variant rule --split dev --output .heybuddy/evaluations/lh-v2-rule-example
uv run python -m personal_agent.evaluation.long_horizon --dataset evals/long_horizon_v2 --ablation all --split dev --output .heybuddy/evaluations/lh-v2-ablation-example
uv run python -m personal_agent.evaluation.long_horizon --report .heybuddy/evaluations/lh-v2-ablation-example
uv run pytest tests/test_long_horizon_v2.py tests/test_thread_routing.py
```

输出目录必须全新且满足原隐私检查；Holdout 仅校验结构和 Hash，不执行路由或评分。`--scripted-action continue|clarify|new` 探测不同路径，默认 continue，路径数量不是实际模型调用预测。

测试覆盖原场景与 Holdout 划分保留、真实状态发布与修订、只读已完成前缀、用户证据与助手文本区分、维护不能修正路由归属、外键、失败/取消、弱规则默认兼容与显式模型选择、Prompt 输入不变、碎片化与污染的区别、零分母、五组完整性、公开汇总无正文和离线重算。默认 pytest 的禁网保护继续保留。

## 离线观察与后续预算

开发集规则基线 21 场景/378 Turn 完整完成，Pairwise micro Precision 0.4776、Recall 0.8369、F1 0.6081；必需澄清遗漏 19/19，误澄清 2/359，coverage 357/359。与旧 8 场景分数不能直接比较数据规模效应，不据此修改冻结标签。

实现提交 `4026ccb` 上的交付重跑与首次规则基线及五组路径计数一致，两个报告均无模型重算一致。无正文汇总见 [v2 开发集与离线对照](../../evals/results/long-horizon-v2-dev-offline-20261105.json)；完整私有轨迹位于 `.heybuddy/evaluations/lh-v2-rule-delivery/` 与 `.heybuddy/evaluations/lh-v2-ablation-delivery/`。原有两处 Episode 未提交修改保留，报告为 git_dirty=true，code_hash 纳入实际使用的依赖。完整回归实际为 1434 passed、3 skipped，Ruff、构建和两个 CLI 帮助检查通过。

五组固定 continue 路径均完成 378 Turn。路由分支数分别为 241、241、241、241、356；四组带状态的组各触发 126 次 Episode 请求和 351/351/351/357 次 Thread 请求。state 组 281 张累计候选卡中 254 张带状态，baseline 为 0。fewshot 最大估算路由输入 4724，超过旧 v1 的 4096 输入上限；真实评测必须重新预检，不能直接扩大旧 profile。

频繁维护明显增加成本。五组的总估算输入依次为 379182、4483513、4721380、5013231、5534015；输出预留依次为 246784、2200576、2200576、2200576、2342912。它们不是 tokenizer 或 Provider Usage，更不是账单；真实选择、Episode 分组和维护次数可能不同。本轮实际外部请求为零。建议先选少量 dev 场景，分开测状态维护与路由，再对照 identity/fewshot/deferred；确认拆分下降且污染、coverage、澄清不恶化后，才扩大真实模型实验。Holdout 保留到方案稳定之后。

旧实验只读重新评分的独立来源、版本与完整指标见 [v1 身份指标重算](../../evals/results/long-horizon-v1-identity-rescore-20261105.json)；原始归档未改写，新增外部请求为零。
