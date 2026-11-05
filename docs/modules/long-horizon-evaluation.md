# Long-Horizon Benchmark Phase 1

更长语料、开发集/Holdout、新澄清/证据关系指标与离线预算预检见 [Thread Routing Benchmark v1 草案](long-horizon-benchmark-v1.md)。旧的五个 Fixture 与 Hash 保留。

## 职责与架构组成

`evaluation/long_horizon/` 是独立的合成评测入口，不改变生产 Agent、Router、Learning 或 Prompt。默认实验类型是 **Offline Contract Test**；显式 `--live --variant model-assisted` 可运行有界 **Real Model Evaluation** 接线 smoke。两者分别报告，均不宣称验证 Memory Learning 效果。

- `models.py`：严格 Pydantic Schema，拒绝额外字段、无时区时间、重复 ID、逆序时间和不存在的证据引用。
- `dataset.py`：加载冻结 Corpus、Gold、Split、Config，验证 `manifest.json` 的规范化 JSON SHA-256。Split 必须恰好划分语料，不可重叠或漏项。
- `routing_eval.py`：`RoutingAdapter.route()` 调用生产 `ThreadRouter.route()`，默认关闭模型选择并提供拒绝任何模型请求的 Provider；Live 宿主可显式传入计量 Provider 开启模型选择，均核对持久绑定。
- `replay.py`：每 Scenario 新建临时 SQLiteStore、固定合成 Agent 和真实 MemoryFS；Conversation 按 conversation_key 创建，来源传入原 RoutingRepository。
- `metrics.py`：基于同话题/同 Thread 的 Pairwise 关系评分，不比较 UUID 与逻辑话题标识。
- `report.py`：私有完整 Bundle、可重算指标、无对话正文的公开 Summary、运行状态与完整性检查。
- `live.py`：LiveConfig、MeteredProvider、LiveRoutingAdapter、独立 LiveBundle、真实调用计量与重算。
- `__main__.py`：默认离线 CLI；模型 Variant 需要显式 `--live` 和输入/输出价格及来源，无授权时在加载 Settings 前拒绝；Oracle-history 继续拒绝。

沿用 SQLiteStore 的 schema 24、Journal 校验与数据库锁，以及 MemoryFS 的原有隐私/发布语义。未复用 `evaluation/p1.py::seed()`，因为该函数先结束 Run，不满足在线 Router 的绑定时机；也未复用现有行为评测评分器，其自动学习契约与 Pairwise 话题评价不同。

## 数据格式与隔离

`evals/long_horizon/corpus.json` 保存 Scenario：scenario_id、description、time_zone、turns、checkpoints、tags。每 Turn 保存稳定 turn_id、带时区 timestamp、user_message、frozen_assistant_message、source、conversation_key。时间按绝对时刻非递减；相同时刻按文件顺序。Asia/Shanghai 和 UTC 不依赖 Windows tzdata；其他 IANA 时区需系统时区数据。

`gold.json` 独立保存 RoutingGold、MemoryGold 和完整 Checkpoint。Corpus 中的 CheckpointTrigger 只有 checkpoint_id、after_turn_id；问题、reference_answer 和证据留在 Gold，避免进入执行上下文。Gold topic 是稳定逻辑标识；模糊项可为 null。MemoryGold 包含有效时间区间、证据与 create/update/delete/retain 动作，仅校验结构和时间，不评分事实语义。

`splits.json` 当前只有 dev，不宣称存在独立 Holdout。五组标签由人工定义，未使用模型生成。`config.json` 冻结 rule/streaming/seed/network=false。修改冻结输入需要显式更新 manifest；Hash 使用排序键、紧凑分隔符和 UTF-8，不依赖文件缩进。版本与哈希分别记录，允许未来实验复用同一历史。

Runner 只接收 Scenario，不接收 Gold。标准助手回复为 teacher-forced 输出；Thread 归属持续使用之前的实际预测，因此本模式是 **Streaming + frozen replies**，不是 Oracle-history。`OracleHistoryAdapter` 只预留宿主历史构造协议，尚无可执行实现；未来必须独立运行与报告，不能混合准确率。

## 代码实际运行路径

1. `python -m personal_agent.evaluation.long_horizon → main()` 解析参数，`dataset.load()` 校验全部文件，再按 Split/Scenario 选择。
2. `report.evaluate()` 验证输出目录隐私，拒绝覆盖，写 running 状态并记录 Git HEAD 与 git_dirty；不加载 Settings、`.env`、生产 SQLite、真实 Memory 或渠道历史。git_dirty=true 表示 HEAD 不能完整代表本地工作区，正式比较应从干净检出运行。
3. `replay.replay()` 为每个 Scenario 创建 TemporaryDirectory，在其中 initialize SQLiteStore、create_agent、initialize MemoryFS。各 Scenario 数据库、Memory 和 Thread 完全独立。
4. 每 Turn `begin_run()` 原子保存原用户消息。评测时钟适配器仅在新建合成数据库中设置 Run/message 时间，不改生产时钟。
5. 在 running 状态调用真实 Router；由原 Repository 创建/选择 Thread 并 bind，Adapter 读取 run_threads 核实结果，保存 reason、新建标记、先前 Thread、歧义、请求数和路由耗时。
6. 追加冻结 AssistantMessage，调用 `finish_run(completed)` 校验 Journal。这里不调用主模型、不执行工具、不跑 Learner/Organizer；历史工具轨迹暂不支持。
7. Checkpoint 到达后记录已完成 turn_ids、当前绑定和真实 Memory 核心块快照，不请求问答。结束释放数据库锁和临时目录。
8. 写 `private-trace.json`（输入、冻结输出、Gold、预测、快照、配置与 Hash）、`trace-hash.json`。校验全量覆盖后写 `summary.json`，最后才写 completed 状态。
9. `--report` 不读取当前语料，不调用 Router/Provider；从保存的 Bundle 与 Gold 验证 Hash 和完整性，再重新计算同一 Summary。

私有文件必须在 Git 整目录忽略且无已跟踪文件的目录，或源码仓库之外。默认 `.heybuddy/evaluations/long-horizon/<id>` 已忽略；不自动修改忽略规则。Summary 仅含 ID、参数、Hash、指标和聚合诊断，可人工导出；整个结果目录仍作为私有目录处理。

## 指标、异常与边界

每 Scenario 独立建立无序 Turn Pair：同 Gold Topic 且同预测 Thread 为 TP，异 Gold 同 Thread 为 FP，同 Gold 异 Thread 为 FN。Gold 模糊项与预测歧义项排除，并单独报告排除数、eligible_turns 和 Clarification Rate；预测弃权不能被解读为完整覆盖的高准确率。当前不从 reason 强行推导 action，所以 Action Accuracy、Fragmentation、Contamination、Resume QA Accuracy 均为 null。

Precision 分母为 TP+FP，Recall 为 TP+FN，F1 分母为 2TP+FP+FN；零分母返回 null，包含 pair_support 和各分母。Micro 汇总计数后计算；Macro 仅平均非 null Scenario，记录有效 Scenario 数。ML001 没有 Routing Gold，路由指标无 support，仅验证 Schema 和快照。延迟为真实 Router 耗时，使用 nearest-rank；P50 至少 2 样本、P95 至少 20 样本。当前总共 15 Turn，P95 为 null。实际请求数为零，缺失 Token Usage 为 unknown，不伪装成零。

规则预测错误属于正常评分结果，不导致 Benchmark 失败。执行异常返回 failed ReplayResult，记录阶段和安全错误类型；未结束 Run 标记 interrupted，取消标记 cancelled 并向上传播，不自动重试。Campaign 保存 failed/cancelled 状态，不发布成功 Summary。进程硬退出保留 running，重新评分拒绝；缺失 Turn/Checkpoint、文件或 Hash 不符均明确失败。原子 JSON 发布不宣称跨文件事务、断电保证或抵御本机恶意篡改。

seed 是保留参数，当前规则路径没有随机采样；Thread/Run UUID、临时目录和耗时每次不同，复现比较应使用 Turn 顺序、规范化 Thread 分区与指标。生产 Thread 创建时间仍是墙钟时间；固定输入按顺序创建，当前规则不使用时间阈值。Oracle-history、长期 QA、Memory Apply/语义评分和完整 Memory Benchmark 均未实现。网络仅在显式 Live 入口开放给既有配置的 Provider。

## 有界真实模型 smoke 的实际路径

1. `__main__.main()` 先检查 Live 开关、Variant 与计价参数。`run_live()` 才用既有 `load_settings()` 读取配置、构造 LiveConfig 和 `AsyncOpenAI(max_retries=0, timeout=10)`，复用生产 OpenAIProvider；不展示密钥或 `.env` 原文。只保存 model、endpoint hostname、显式 context window。
2. `live.evaluate_live()` 选择带 routing 标签的 Scenario，不跑 ML001。共享根预算下，每次重复先跑 rule 基线再跑 model-assisted，每 Scenario 仍新建隔离环境。最多 5 次重复、5 次请求、20000 预留 tokens、10 元人民币；单次估算输入最多 2048，输出沿用生产 1024，总时限 120 秒。
3. `LiveRoutingAdapter` 只启用真实 ThreadRouter 的 model_selection。原规则优先级与 Prompt 不变，因此现有四个 Fixture 每轮只在 TR004 触发一个模型分支。不会强迫所有 Turn 请求模型。Gold 不交给 Adapter/Provider，助手回复冻结。
4. `MeteredProvider.generate()` 在外部发送前检查输入与预算、通过既有 Budget.reserve 预留额度，并原子写 `live-progress.json`。完成后保存实际 usage、纯可见回复与耗时；不保存 Provider 原始 reasoning。输入/输出按所提供 CNY 单价计算费用上界估算；缓存/空闲折扣不主动推测，reasoning 不重复加入 output。
5. 请求错误、超时/取消、缺失 usage、工具调用或实际 token/费用越界设置停止标记。即使原 Router 把 ProviderError/超时转成 selection_unavailable，Adapter 仍检查停止标记并终止 Campaign。输出 JSON 非法但 Provider 请求完整时，保存 selection_unavailable 为决策失败统计；不将它算作模型选择成功，也不自动重试。
6. 完整结束时写 `live-trace.json`、Hash、Public Summary 和 completed 状态；失败保存 progress 和 failed/cancelled 状态，不发布成功 Summary。进程硬退出留 running，不自动恢复或重发。实际 Usage 缺失标 unknown，停止后续调用，不宣称已经撤销费用。
7. Live Summary 按 Variant/重复/Scenario 保存指标，分离规则与模型决策 support、clarify 与 selection_unavailable。记录 Corpus/Gold/Config/LiveConfig Hash、Git HEAD/dirty、评测和关键生产实现的 code_hash 及真实 Prompt Hash。`--report` 自动识别 Live Trace，在不加载配置/不调用模型的情况下核对完整性、调用数、Usage/成本和预算并重算。

配置预算是请求前估算与请求后核对，不是服务商账单硬封顶；输入估算不等于 tokenizer，报告金额不冒充服务商账单。所有重复只是同一组小样本的稳定性 smoke，正式语义能力 Benchmark 留待下一阶段。

已核实 2026-11-05 当前配置为 DeepSeek 官方端点的 deepseek-flash；本轮按高峰未缓存输入 2 元/百万 tokens、输出 8 元/百万 tokens 预留，来源为 [DeepSeek 官方价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)。最高预留约 0.06144 元。价格不会隐式套用于其他模型/端点，运行者须明确提供核实过的计价参数。

```powershell
uv run python -m personal_agent.evaluation.long_horizon --live --variant model-assisted --repetitions 5 --max-requests 5 --max-tokens 20000 --max-cost-cny 10 --input-price-cny 2 --output-price-cny 8 --price-source https://api-docs.deepseek.com/zh-cn/quick_start/pricing/ --output .heybuddy/evaluations/lh-live-smoke
uv run python -m personal_agent.evaluation.long_horizon --report .heybuddy/evaluations/lh-live-smoke
```

`tests/test_long_horizon_live.py` 仍为 Offline Contract Test，使用 Fake Provider 和 MockTransport 覆盖真实 SDK/Provider 映射、共享预算、缺失 Usage、错误/不完整回执、取消、失败报告、Gold 与公开摘要隔离及指标重算；不发起付费 API 请求。

### 2026-11-05 真实 smoke 结果

实现提交 `30c2b64` 上运行四个 routing Fixture，两个 Variant 各重复五次，共 40 个 Scenario Trial、120 Turn。仅 model-assisted 的 TR004 第三条触发模型，共 5 次真实请求；回执完整，无 API/选择解析错误，报告重算一致。实际 Usage 为输入 2219、输出 2154 tokens，输出已含 reasoning；按本次较高单价估算费用上界 0.02167 元，未达到 10 元预算。模型请求 P50 约 2.366 秒；只有 5 个样本，P95 不报告。

规则和模型辅助 Variant 的 Pairwise F1 均为 1.0，但模型分支 5 次均返回 continue，未标记澄清；Gold 模糊项上的 Clarification Rate 为 rule 5/5、model-assisted 0/5。模糊项排除在 Pairwise 之外，因此总 F1 不能反映此问题。它是同一个合成输入的五次重复，不是五个独立语义样本；不改 Prompt/规则/Gold 来追分，保留为后续正式 Benchmark 的歧义维度线索。

无正文的可提交记录见 [smoke 汇总](../../evals/results/long-horizon-smoke-20261105.json)。完整私有轨迹保留在被忽略的 `.heybuddy/evaluations/lh-live-smoke-20261105/`；原有 Episode 未提交改动继续保留，报告 git_dirty=true，并记录参与评测实现的 code_hash。未运行 Memory Learning、主模型问答或新正式语料。

## 阅读与验证

从仓库根目录执行：

```powershell
uv run python -m personal_agent.evaluation.long_horizon --list
uv run python -m personal_agent.evaluation.long_horizon --mode routing --variant rule --split dev --output .heybuddy/evaluations/lh-demo
uv run python -m personal_agent.evaluation.long_horizon --report .heybuddy/evaluations/lh-demo
uv run python -m personal_agent.evaluation.long_horizon --scenario TR001 --seed 7
uv run pytest tests/test_long_horizon.py tests/test_thread_routing.py tests/test_learning.py tests/test_harness.py
```

输出目录必须是全新目录。`tests/test_long_horizon.py` 覆盖 Schema/冻结校验、真实路由生命周期、重放顺序、隔离、Gold 不进入路由、Pairwise 错误评分、歧义/零分母、取消/异常、不完整报告和公开摘要隐私，沿用 conftest 的无凭据、禁止外网保护。

下一阶段应先冻结更丰富的人工标注语料与独立 Holdout，加入显式 action 定义和跨来源样例，明确弃权的 coverage 约束；再实现独立 Oracle-history 实验与人工语义复核，正式能力 Benchmark 的付费预算另行授权。

当前语料的模型分支覆盖与两阶段预算依据见[真实模型评测方案](../long-horizon-live-evaluation-plan.md)；首轮 smoke 已授权接入验证，第二轮正式 Benchmark 仍待后续建设。
