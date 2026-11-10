# HeyBuddy Sequence Extension v1

这是独立于原 v0.4 的合成诊断集：60 个场景、30 个家族、118 个检查点。每个家族有两个相关变体，不能将它们当作 60 个独立随机样本。按家族划分 42 个开发场景和 18 个保留场景；初次真实实验只用开发集。作者为编码 Agent，`human_reviewed=false`，目前不是可直接用于简历的泛化能力成绩。

已完成的真实 API 诊断及失败解释见 [RESULTS](RESULTS.md)，累计费用上界估计 ¥0.427512；未调参、未运行保留集。

## 覆盖范围

| 轨道 | 20 个场景覆盖的 10 类问题 | 新增观察 |
| --- | --- | --- |
| Routing | 旧主题精确提及、旧主题语义改写、科研/面试来回切换、共享关键词、新建同类项目、模糊指代、历史隔离、显式 ID、状态尾部信息、当前主题偏置 | 8/55 个干扰 Thread；真实最近 24 个候选、当前 Thread 补入与 LLM 最多 12 个候选；真实绑定后的历史可见性 |
| Memory | 申请到入职、技术决定更正、尚未决定、局部与长期偏好、一次午饭与长期偏好、助手建议非用户事实、撤销偏好、多周行为与明确偏好、重复与改名、否定约束与撤销 | 同一 MemoryFS 连续更新，每个检查点检查实际文件，而非只判 Extractor 的候选 |
| Proactive | 原话发现与假设负例、已聊进展、完成、拒绝跟进、安静时段、过期、多个事项竞争、助手建议非计划、普通更新不恢复、新计划恢复 | 不提供预制机会，实际 Scout 创建、修改 Proposal；再运行生命周期及单次 Attention |

每个轨道的最后三个家族整体保留。变体会改变候选规模、证据确定性、局部作用域、否定或完成状态、事项紧迫性；部分变体仅改变观测时刻。这是有意的对照，数量不代表同等多样性。

## 数据格式与输入输出

`inputs.jsonl` 每行一个场景，顶层为 `case_id/track/family/split/input`。元数据只供选题与汇总，不进入模型。`input.steps` 按带时区的时间顺序披露；`gold.jsonl` 单独保存同 ID 的 `checks`，每步一个检查。

Routing 的输入含 `threads`、`active`、带原话的 `history`、以及 `steps[{id,at,text}]`。先建立隔离 Thread 库；每条新消息进入实际 Router，绑定结果会影响后续路由。输出包括 action、实际 Thread ID、规则原因、最近候选、实际模型候选，以及 Thread 隔离后的历史。`context_has/context_absent` 只检查上下文可用性，不能代替下游任务成功率。

Memory 的输入含 `memory_files` 和 `steps[{id,at,text,assistant?,learn}]`。从空记忆起步，将每步保存为真实 completed Run；`learn=true` 调用实际 Learning 服务，保留之前写入的文件。输出为逐步文件状态、校验/发布结果和模型整合决定。Gold 的 `present` 是“每组至少命中一项”的词面检查，`absent` 为不应出现的片段，另有重复文件上限；并非语义蕴含评审。

Proactive 的输入只有 `steps[{id,at,text?,assistant?,attention}]`。开始时 Proposal 库为空，先消费新增原话；`attention=true` 仅额外尝试一次 Attention，不能重复询问直到得到理想答案。输出包括实际 Proposal、来源事件 ID、生命周期/覆盖状态、决策和 gate。Gold 的 `opportunities` 按主题片段和真实来源匹配，不依赖模型生成的英文 topic_key；`states/followups/actions/selected_any` 分别检查状态、是否已覆盖、动作与选中事项。部分低紧迫机会允许多个合理动作，不能将此成绩理解为单一正确用户偏好。

三个可直接查看的例子：

1. `seq-r-01-1`：科研 Thread 在 55 个更新的干扰 Thread 之后，用户请求继续科研。Gold 要求回到 `research`，并恢复 SQLite 约束，不混入面试专属内容。当前 Router 若召回不到，应保留失败，不为它预先放回目标。
2. `seq-m-01-0`：第一天“正在申请实习”，第二天“已获 offer，准备入职”。检查第一步保留申请目标，第二步实际文件包含新状态，且不原样留下旧的当前状态。
3. `seq-p-02-1`：先说 Benchmark 没开始，后说“还没开始，今天太忙了”。期待 Scout 将本轮关心标为 covered，而非完成；到复查时无新关心动作。另有 `seq-p-07-0/1` 对调面试和 Benchmark 的紧迫性，检查最终选中哪件事。

## 如何执行

```powershell
uv run python evals/heybuddy_bench/sequence_v1/build.py
uv run python -m personal_agent.evaluation.heybuddy_bench.sequences --validate
uv run python -m personal_agent.evaluation.heybuddy_bench.sequences --stage dev --root .cache/sequence-contract
```

前两步重建/校验冻结文件，第三步用固定、与 Gold 无关的离线响应，只检查接线，不输出能力准确率。模块说明与真实调用路径见 [sequence-evaluation](../../../docs/modules/sequence-evaluation.md)。

用户本轮已授权真实调用累计不超过 ¥20，显式入口如下。两个阶段共用同一个 `--root` 和持久账本，禁止为失败重置账本或另开额度。

```powershell
uv run python -m personal_agent.evaluation.heybuddy_bench.sequences --stage smoke --live
uv run python -m personal_agent.evaluation.heybuddy_bench.sequences --stage dev --live
```

使用可信配置加载器取得现有模型，不显示 `.env`，不读真实聊天库。模型/HTTPS 主机必须与冻结 `live_protocol.json` 一致；无工具、无 Sender、无 SDK 重试。DeepSeek Flash 价格按 2026-11-10 官方高峰未命中输入 ¥2/百万、输出 ¥8/百万预留；缓存折扣不计入，因此是费用上界估计，最终账单以供应商为准。每次请求先按输入 UTF-8 字节加余量及最大输出预留，再用已知 usage 结算；未知 usage 冻结整个账本。原始请求/响应、文件状态和逐题预测仅保存到忽略目录，提交的结果不含这些正文。

## 评分与边界

报告同时列出“全部检查通过的场景数”“全部断言通过的检查点数”。Routing 另列原始 Recent Recall@24、补入当前 Thread 后的有效候选召回，以及仅在模型实际被调用时的候选召回；后者不代表全库召回。缺失、超时或中断后的检查点留在分母。检查器和 Gold 在真实实验前冻结，不根据结果改标签。

当前仍有以下限制：

- 不改造任何公开数据；新集全部是原创合成情景，原集公开来源与指标保持原样。需要人工复核标签、更多独立主题和真实授权日志，不能用变体数冒充样本独立性。
- Routing 初始 Thread 卡由数据定义，未测试 Organizer 从原始全量对话组织 Thread 的质量；历史可见性是下游代理指标，没有运行开发任务或长期 QA。
- Memory 采用明确学习检查点，未测 Priority/Batch/空闲调度、Episode 自动边界或大规模历史累积；保留的多周情景也只有四次观测，不能证明稳定行为规律学习。词面检查会误杀改写，也可能漏掉语义错误或历史限定，不能报告为 Faithfulness/F1。
- Proactive 真实使用 Scout/Attention，但关闭 Composer 和随机社交时间窗，保留默认 6 小时通用复查、7 天寿命、23–8 点安静时段。`attention` 检查点由数据控制，没有模拟整日调度、真实发送、打扰成本学习或用户反馈；竞争只有两个机会，未覆盖数十个机会在候选上限外的饥饿问题。
- 没有加入 Context Compaction、工具恢复/注入防护、全链路任务成功率、跨模型/多随机种子对照；本集先扩展最直接的三个缺口。初次开发结果仅用于发现问题，保留集不用于调参。
