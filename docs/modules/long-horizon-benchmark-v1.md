# Thread Routing Benchmark v1 草案

## 职责与组成

在 [Phase 1 基础设施](long-horizon-evaluation.md) 上增加更长的合成场景、按场景划分的开发集/Holdout、澄清与证据关系指标，以及离线预算预检。生产 Router、Prompt、Agent Loop、Memory Learning 和 SQLite schema 均不修改。

数据入口为 `evals/long_horizon_v1/`，12 场景/216 Turn，开发集 8 场景/144 Turn、Holdout 4 场景/72 Turn，全部有明确合成来源。Gold 是 Agent 显式编写的待人工复核草案；标签在首次规则评分前冻结，不按 Router 输出反标。当前不是已验证的真实模型能力 Benchmark。

`models.BenchmarkProtocol` 保存作者/审阅状态、评分版本、场景长度和划分约束；`dataset.Dataset` 校验 protocol 与数据一致，`load()` 校验其独立 Hash。原 Phase 1 没有 protocol 时保持兼容。报告 Bundle 保存 protocol，重算不用访问当前语料文件；公开报告包含 annotation_status、协议 Hash、评分版本和评测代码 Hash。

`metrics.py` 提供版本 `routing-relations-v1` 的加法指标，原 Pairwise 公式与零分母处理不变。`preflight.py::BudgetProbe/preflight()` 仅用固定选择探测生产 Router 的模型分支与估算输入量。CLI `--preflight` 与 Live/list/report 互斥，始终不加载模型配置或 Key。

## 场景、Turn、Trial 与输入输出

场景是一段完整的测试故事，例如花店手机端与网页端两个独立项目交替讨论，再跨天回到旧项目；一个场景可包含多个逻辑话题/Thread，因此场景不等于 Thread。当前每场景 18 Turn，场景之间新建数据库、Conversation、Memory 和 Thread，互不继承状态。

Turn 是一轮用户输入及其冻结助手回复，对应一个持久化 Run。按时间逐轮推进，先保存用户消息、执行路由，再写固定回复；不是一次把未来 18 轮都交给 Router。模型请求是另一种计数：规则能决定时不请求模型，所以一轮 Turn 不一定产生一次付费调用。

Trial 是某个 Variant 对某个完整场景的一次独立重放。本次开发集 8 场景，rule/model-assisted 各一次，计划共 16 Trial、288 次 Turn 重放；付费调用只来自 model-assisted 中需要模型选择的 Turn，最多 136 次。

| 层级 | 输入 | 输出 |
| --- | --- | --- |
| 一次实验 | 冻结 Corpus、Split、配置；Gold 单独留给评分器 | 每场景 Trace、每 Variant 汇总、用量/延迟/预算与完整性状态 |
| 一轮 Router | 当前用户文本、宿主保存的 Thread/历史状态 | 实际 Thread 绑定、先前 Thread、新建/选择理由、歧义标记 |
| 一次模型选择（仅当规则未决） | 当前输入、active Thread ID、至多 12 张 Thread 标题/有界状态卡、最近最多 4 条消息片段；无工具、无 Gold | 结构化 continue/select/new/chat/clarify，由真实 Router 校验并保存绑定 |
| 评分 | Prediction Trace + 独立 Gold 的话题、允许动作与历史证据引用 | Pairwise、澄清遗漏/误报、coverage、证据关系与连续失败统计 |

数据库保留完整 Journal，不代表每次路由请求都会看到完整历史。当前重放也不运行后台 Organizer/Learning，Thread 状态可能尚为空；主模型自然回答、长期记忆学习和回答正确性不在本轮评测范围。冻结回复是控制实验历史的输入材料，不是模型本轮生成的成绩。

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

历史证据的预测归属本身可能已经出错，当前选择也可能因此与旧引用冲突。关系失败不等价于当前 Turn 单独选错，更不能直接归因某一轮；这里不把它包装成逐回合绝对话题准确率。应同时查看原 Trace、Pairwise、coverage 和各类 support。

协议与 Gold 语义没有第三方复核，场景与冻结回复的自然性仍有限。Holdout 仅按场景隔离，当前没有执行路由或评分；这是本地可读的内部 Holdout，不是盲测。人工修正必须带版本和原因，不能看模型结果后改标签而沿用同一冻结版本。

## 本轮离线观察与预算建议

开发集规则基线共 144 Turn，执行完整。Pairwise micro F1 约 0.6142，macro F1 约 0.6042；必需澄清遗漏 7/7，过度澄清 1/137，显式历史关系正确 200/335。最长连续关系失败段为 16 Turn。上述结果是 Offline Contract Test 与规则表现，不能当作真实模型评测，也不据此调整标签/Prompt。

提交 `44d3b5a` 上的交付重跑与首次基线指标一致，报告重算一致；无正文汇总见 [v1 开发集离线结果](../../evals/results/long-horizon-v1-dev-offline-20261105.json)。完整私有报告位于被忽略的 `.heybuddy/evaluations/lh-v1-rule-delivery/`，预算路径位于 `.heybuddy/evaluations/lh-v1-budget-delivery/`。报告保留 Git dirty 标记（原有 Episode 修改）和精确评测代码 Hash。

三个固定模拟路径的模型分支次数为 continue 111、clarify 111、new 63；单次估算输入最高 2782 tokens。它们只有预算用途，不能看作真实模型调用次数或能力。以每请求估算输入上限 4096、输出 1024 规划，开发集单次最多 136 请求、696320 预留 tokens；Holdout 单次解析上限 68 请求、348160 tokens，不需要执行 Holdout 得到此上限。

若沿用上一轮已核实的未缓存输入 2 元/百万、输出 8 元/百万作为暂定计价假设，开发集单次上界估算为 `(136×4096×2 + 136×1024×8)/1000000 = 2.228224` 元；建议下一轮先做开发集一次、最高 3 元的独立实验。此处只是提案，执行前需复核端点价格并授权。两个 Split 各重复 3 次的同条件上界约 10.027008 元，不应宣称必然低于上一轮 10 元 smoke 预算。

输入估算不是 tokenizer。固定模拟路径不能覆盖所有模型选择；实际输入超规划上限应停止而不是自动提额。原 LiveConfig 的 5 次/20000 tokens smoke 限制继续保留。用户随后已授权开发集一次、人民币 3 元的独立测试；新增 DevLiveConfig 专用配置承载该授权，Holdout 不随开发迭代反复跑。

## 已授权开发集单次测试

`live.DevLiveConfig` 继承共用的请求元数据，单独限定 repetitions=1、最多 136 请求、696320 预留 tokens、每请求估算输入最多 4096 tokens、输出仍为 1024、人民币 3 元、总时限 1800 秒。原 smoke 类型及其序列化字段不变，旧的配置 Hash 和 Trace 继续可读。LiveBundle 分别使用 live-smoke-1/live-dev-1 保存预算类型，重算核对预算类型与版本一致。

CLI 显式 `--budget-profile routing-dev-v1` 才选此配置；省略时保持 smoke。`validate_live_scope()` 在创建输出和请求前核对完整 routing-v1-draft1 开发集 LH001–LH008、每场景 18 Turn；拒绝 Holdout、单个场景、其他语料或重复次数增加。模型参数和原 Router 的 10 秒单次超时不改，SDK 不自动重试，遇到 API 错误、回执不完整、未知 Usage 或限额问题仍停止整个测试，不自动提高输出上限或重跑。

`evaluate_live()` 在发送任何付费请求前保存私有 live-metadata.json，包含选择范围、配置、数据/协议/代码 Hash 和 Git 状态；因此失败也能核对实验配置。live-progress.json 保存逐步调用回执与已完成/部分重放记录，只有完整结束才发布 completed Summary。源数据与 Gold 不随结果改动。

```powershell
uv run python -m personal_agent.evaluation.long_horizon --dataset evals/long_horizon_v1 --split dev --live --variant model-assisted --budget-profile routing-dev-v1 --repetitions 1 --max-requests 136 --max-tokens 696320 --max-cost-cny 3 --input-price-cny 2 --output-price-cny 8 --price-source https://api-docs.deepseek.com/zh-cn/quick_start/pricing/ --output .heybuddy/evaluations/lh-v1-live-dev
```

参数中的单价按当前 DeepSeek 官方价表复核，仍按较高的未缓存输入/输出价估算，金额不冒充服务商账单。Gold 的 agent-authored provisional 状态保留；一次开发集实验不构成独立泛化结论。

### 2026-11-05 开发集真实测试回执

提交 `9b301f4` 上按授权参数实际尝试一次开发集实验，运行状态为 failed，不能报告模型整体分数。规则 Variant 的 8 场景/144 Turn 全部完成，指标与之前离线基线一致。模型辅助 Variant 在首个场景 LH001 的第 7 Turn 停止，此前完成 6 Turn；其余模型场景没有运行，Holdout 没有运行。

共 4 次真实模型请求：前 3 次完成，第 4 次返回 incomplete_response。该请求实际 output_tokens=1024、reasoning_tokens=1024，等于配置的输出上限，未取得完整路由结果。原 MeteredProvider 没有保留 ProviderDiagnostics 的 incomplete_reason 枚举，因此这里只记录可核对的用量与错误码，不反推或伪造服务商细分原因。该现象提示需要检查路由选择的推理/输出额度，不能直接解释为模型选错话题。

总 Usage 完整：输入 1696、输出 1496 tokens；按本次较高单价估算费用上界 0.01536 元，没有耗尽 3 元总预算。按预先定义的错误停止规则终止，不自动重试、提高输出额度、修改 Prompt 或改 Gold。部分模型轨迹不计算完整开发集 F1；未完成结果的成功重算入口明确拒绝。

无原始正文汇总见 [开发集真实失败回执](../../evals/results/long-horizon-v1-live-dev-20261105.json)。完整私有元数据、逐步回执与部分 Trace 保留在 `.heybuddy/evaluations/lh-v1-live-dev-20261105/`。下一步应先独立核对路由选择的推理/输出配置，再制定新的受控实验，不能把重新运行冒充本轮续跑成功。

## 独立的非思考开发集实验

用户随后授权核对参数并进行新的受控测试。DeepSeek [思考模式文档](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)说明默认开启思考（默认 high），Responses API 可用 `reasoning.effort=none` 关闭。本次选用显式关闭思考的新实验配置；保持相同 deepseek-flash、Prompt、Gold、冻结回复、1024 输出上限与开发集范围，不把它标作生产默认配置的结果，也不修改生产 Provider/Router。

`NoThinkingDevConfig` 沿用 DevLiveConfig 的 1 次开发集、136 请求、696320 预留 tokens、输入上限 4096、人民币 3 元和 1800 秒总时限，仅增加固定 reasoning_effort=none 与独立 profile。`validate_live_scope()` 额外核对模型为 deepseek-flash、hostname 为 api.deepseek.com；其他端点/模型未经本次参数核实，不接受这个 profile。

CLI `--budget-profile routing-dev-v1-no-thinking` → `run_live()` 创建专用配置 → `MeteredProvider.generate()` 在调用既有 Provider 时显式传 reasoning_effort="none" → 原 OpenAIProvider 映射为 Responses 的 reasoning.effort。记录实际 request_options、独立 LiveConfig Hash 和 live-dev-no-thinking-1 Trace 版本，防止与默认思考实验混算。原 smoke/default dev 请求继续省略 reasoning 参数，旧配置 Hash 保持可读；不自动探测或重试其他思考配置。

同时补存 ProviderError 中已规范化的 ProviderDiagnostics（状态、细分原因、usage），不保存 SDK 异常体或原始 reasoning。旧失败的细分原因仍保持 unknown，不反向补造。`tests/test_long_horizon_live.py` 用 MockTransport 核对 none 的实际 wire 参数、完整开发集与重算、默认请求不变、错误原因落盘及模型/端点隔离，仍无真实网络。

```powershell
uv run python -m personal_agent.evaluation.long_horizon --dataset evals/long_horizon_v1 --split dev --live --variant model-assisted --budget-profile routing-dev-v1-no-thinking --repetitions 1 --max-requests 136 --max-tokens 696320 --max-cost-cny 3 --input-price-cny 2 --output-price-cny 8 --price-source https://api-docs.deepseek.com/zh-cn/quick_start/pricing/ --output .heybuddy/evaluations/lh-v1-live-dev-no-thinking
```

本实验与上一次失败记录分开保存，不续跑旧数据库，不修改已冻结标签或重写旧结果；发生不完整回执或其他停止条件时仍停止，绝不因已切换参数就把失败当作成功。Holdout 保持不运行。

### 2026-11-05 非思考实验结果

提交 `abf1d9f` 上完整完成 16 Trial、288 次 Turn 重放（rule/model-assisted 各 8 场景/144 Turn）。真实请求 77 次，全部完成，无 selection_unavailable；逐条请求参数记录为 reasoning_effort=none，SDK wire 映射由 MockTransport 测试核对，实际 reasoning_tokens 合计为 0。输入 34046、输出 2681 tokens，按本次较高单价估算费用上界 0.08954 元；预留金额 0.898906 元不是实际用量金额，均未达到 3 元限额。模型请求 P50 约 0.966 秒、P95 约 1.445 秒（support=77）。

| 指标 | rule | model-assisted，显式非思考 |
| --- | ---: | ---: |
| Pairwise micro Precision | 0.4729 | 0.8032 |
| Pairwise micro Recall | 0.8762 | 0.5843 |
| Pairwise micro F1 | 0.6142 | 0.6765 |
| Pairwise macro F1 | 0.6042 | 0.6574 |
| 必需澄清遗漏 | 7/7 | 6/7 |
| 不应澄清却标记澄清 | 1/137 | 2/137 |
| 无歧义 Gold 的路由 coverage | 136/137 | 135/137 |
| 显式证据关系一致性 | 200/335 | 228/330 |
| 最长连续关系失败段 | 16 Turn | 8 Turn |

模型辅助的 Pairwise 误合并较少，但同话题被拆散的问题更多（FN=212，对照 rule 为 64；有效 Pair support 因弃权不同，为 1074/1091）。不能只报告 F1 上升而隐藏 Recall 下降和 coverage 差异，澄清问题也仍明显。不同场景效果不一致：LH002/LH003 的 F1 低于对应规则基线。当前是一遍开发集合成草案评测，无统计显著性或泛化结论；默认思考实验未完成，不能与本轮作完整能力对比。

规则基线与之前离线结果一致，完整报告无模型重算一致。可提交的无正文记录见 [非思考开发集结果](../../evals/results/long-horizon-v1-live-dev-no-thinking-20261105.json)，私有完整 Trace 保留在 `.heybuddy/evaluations/lh-v1-live-dev-no-thinking-20261105/`；上一轮失败记录保留，Holdout 未运行。未按分数修改 Gold、Prompt、规则或生产模型配置。

## 阅读与验证

```powershell
uv run python -m personal_agent.evaluation.long_horizon --dataset evals/long_horizon_v1 --list
uv run python -m personal_agent.evaluation.long_horizon --dataset evals/long_horizon_v1 --split dev --variant rule --output .heybuddy/evaluations/v1-rule
uv run python -m personal_agent.evaluation.long_horizon --report .heybuddy/evaluations/v1-rule
uv run python -m personal_agent.evaluation.long_horizon --dataset evals/long_horizon_v1 --split dev --preflight --output .heybuddy/evaluations/v1-budget
uv run pytest tests/test_long_horizon.py tests/test_long_horizon_live.py tests/test_long_horizon_v1.py
```

`tests/test_long_horizon_v1.py` 覆盖 12 场景/时间与引用、8/4 划分、协议冻结与标注状态、遗漏/过度澄清、可选澄清/零分母、连续关系失败、无 Gold 的真实 Router 预算路径和带协议的报告重算。Holdout 只做静态数据校验，不输出 Holdout 预测或评分。沿用网络禁用保护，不读真实 `.env` 或调用模型。
