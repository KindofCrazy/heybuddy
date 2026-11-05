# Long-Horizon Benchmark Phase 1

## 职责与架构组成

`evaluation/long_horizon/` 是独立的合成评测入口，不改变生产 Agent、Router、Learning 或 Prompt。当前唯一实验类型是 **Offline Contract Test**，只验证机制与固定规则表现，不构成 Real Model Evaluation 或 Memory Learning 效果验证。

- `models.py`：严格 Pydantic Schema，拒绝额外字段、无时区时间、重复 ID、逆序时间和不存在的证据引用。
- `dataset.py`：加载冻结 Corpus、Gold、Split、Config，验证 `manifest.json` 的规范化 JSON SHA-256。Split 必须恰好划分语料，不可重叠或漏项。
- `routing_eval.py`：`RoutingAdapter.route()` 调用生产 `ThreadRouter.route()`，关闭模型选择并提供拒绝任何模型请求的 Provider，核对持久绑定。
- `replay.py`：每 Scenario 新建临时 SQLiteStore、固定合成 Agent 和真实 MemoryFS；Conversation 按 conversation_key 创建，来源传入原 RoutingRepository。
- `metrics.py`：基于同话题/同 Thread 的 Pairwise 关系评分，不比较 UUID 与逻辑话题标识。
- `report.py`：私有完整 Bundle、可重算指标、无对话正文的公开 Summary、运行状态与完整性检查。
- `__main__.py`：离线 CLI，未知 Variant、Oracle-history、`--live` 均拒绝。

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

seed 是保留参数，当前规则路径没有随机采样；Thread/Run UUID、临时目录和耗时每次不同，复现比较应使用 Turn 顺序、规范化 Thread 分区与指标。生产 Thread 创建时间仍是墙钟时间；固定输入按顺序创建，当前规则不使用时间阈值。真实模型、Oracle-history、长期 QA、Memory Apply/语义评分、网络和完整 Memory Benchmark 均未实现。

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

下一阶段应先冻结更丰富的人工标注语料与独立 Holdout，加入显式 action 定义和跨来源样例，明确弃权的 coverage 约束；再实现独立 Oracle-history 实验、固定模型配置/预算/调用记录及人工语义复核，得到授权后才运行 Real Model Evaluation。
