# 连续场景 Benchmark

## 职责与架构组成

`evals/heybuddy_bench/sequence_v1/` 保存原创合成输入、独立 Gold、冻结 manifest、费用协议和可复现生成器。它补充现有 snapshot benchmark，不替换旧分数或生产策略。数据覆盖、格式、例子与限制见该目录的 [README](../../evals/heybuddy_bench/sequence_v1/README.md)。

`evaluation/heybuddy_bench/sequence_data.py` 校验文件 hash、严格输入字段、家族分割、时序与 Gold 引用，并在预测结束后执行固定词面/状态断言。`sequence_replay.py` 负责只读输入的逐步回放，复用 `adapters.py` 的合成 Run、MemoryFS 初始化和固定时钟；每个场景只有一套临时 SQLite/MemoryFS，场景之间隔离。`sequences.py` 是独立 CLI，负责开发集选择、冻结、私有产物、记账与报告；付费路径复用 `experiment.py::SessionBudget/ExperimentProvider` 和 `runner.py::Meter`，不增加 SDK 依赖。

## 代码实际运行路径

1. `sequences.main()` → `sequence_data.validate()` 核对冻结输入与标签。`--validate` 直接结束；默认路径不用真实 Provider、不加载应用 Settings。开发集选择不读取 Gold 内容，保留家族不进入初次实验。
2. 只有 `--live` 进入可信 `load_settings()`，仅用现有 Key、模型和端点，不装配真实应用、渠道或用户数据库。HTTPS 主机和模型必须匹配 `live_protocol.json`；上限不能超过用户授权的 ¥20。会话锁、持久预算和实现/协议 hash 横跨 smoke、dev 两阶段；未知用量、已存在阶段或冻结后代码变化均停止。
3. `execute()` → `run_sequences()`：逐场景建立临时目录、SQLiteStore、SessionManager。`Meter` 先预留每次请求，再调用 Provider；写入私有预测并刷新。不把 case_id/family/split/Gold/未来步骤发送给模型。
4. Routing：`seed_threads()` 初始化卡片及绑定历史 → 每步 `route_step()` → `ThreadRouter.route()` → 实际持久绑定 → `SQLiteThreadHistory.page()` 读取被选择 Thread 的历史。保留完整库、近期 24 个候选、当前 Thread 补入及实际 LLM 卡片的区别，规则直接命中也会被记录。
5. Memory：`learning_service()` 在输入之前初始化 LearningRepository、ReflectionService 和 MemoryFS → 每步保存一个带原始角色/时间的 completed Run → 显式检查点调用 `ObservedLearning.learn(run_ids=[新增Run])` → 实际提取/引用校验/整合/发布 → 保存文件状态。后一步读取前一步的实际文件；不替换成 Gold 状态，不调用人工 Reflection Apply，也不写真实用户 Memory。
6. Proactive：初始化 `ProactiveRuntime` 的消费水位 → 每步添加实际 completed Run → `proactive_step()` 有界消费 pending 原话，调用真实 OpportunityScout 和 publish → 检查点额外调用一次 Runtime，使 gate/expiry/due/Attention 决定是否行动。读取实际 Proposal 和 decision payload，将真实 Run 来源映射到合成 step ID。始终 Shadow，Composer 和社交随机时间窗关闭，不创建 Sender。
7. `sequence_data.score()` 在推理完成后读取独立 Gold。逐步检查动作、目标、实际文件/生命周期，缺失步骤仍计失败；分别报告场景与检查点通过数、Routing 召回数，不将离线固定响应计为能力成绩。真实请求/响应、文件内容、预测保存在忽略目录，公共结果只导出 hash、计数、断言和安全错误码。

## 异常与边界

每个场景 240 秒上限，源消费最多 16 单元；模型/发布失败不通过重复调用寻找理想输出。生产服务可能把预算异常转换为普通失败，所以回放还检查全局 exhausted/usage_complete；未知账单用量立即停止后续请求。SDK 重试为零，输出/推理用量沿用 Provider 计数，reasoning 已含在 output，不重复计费。

正式 Gold 尚未人工复核。词面检查不能判定历史/当前语义、完整 Faithfulness 或用户满意度，细粒度失败用于人工审阅，不等于模块真实错误率。本实现不评估自动 Learning 时机、全量候选检索的改进算法、真实主动投递、Compaction 或工具规划。工作摘要、Memory 和历史证据的生产权限均不改变。

## 阅读与验证

最小入口是 `uv run python -m personal_agent.evaluation.heybuddy_bench.sequences --validate`；离线全部开发回放加 `--stage dev --root .cache/sequence-contract`。真实调用必须显式加 `--live` 并复用同一费用账本。`tests/test_heybuddy_sequences.py` 离线检查时序/标签隔离、家族划分、真实连续 MemoryFS 更新、候选缺失、历史隔离、Scout/coverage、一次 Attention 与中断分母；固定 Provider 只用于机械接线验证。
