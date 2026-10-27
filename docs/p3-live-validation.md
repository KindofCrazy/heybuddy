# P3 真实 API 效果检查（2026-10-28）

## 结论与范围

用户本轮明确授权真实 API 测试。使用现有可信环境配置的 deepseek-flash，未输出或提交 Key/.env/端点详情。基准提交为 664532a698f475f20f22b8fdd0ff7f15f7343a07；角色包、固定语料和 rubric 未修改。原始回答、合成 SQLite/Memory 和逐请求记录在被忽略的 .heybuddy/evaluations 下，只提交无正文的 [机器汇总](p3-live-results.json)。

统一 2048 总输出 tokens 的 E0/E1/E2 对比全部完成：各 13 场景、32 轮、33 次请求，合计 96 轮/99 次真实请求；工具场景每组多一次请求。角色的证据意识、反例兴趣和现实感官边界可观察到，但这次不能证明 E2 稳定优于 E1。模拟状态的真实生成和关闭通过独立两事件 smoke 验证，尚不代表长期使用质量。

## 实验条件与预算

两次完整对比分别使用 512 与 2048 输出上限，不混合排名；总输出包含 reasoning。每组同一模型、1048576 配置窗口、60 秒请求超时、工具、固定时间、前文和检索夹具，用 condition_hash 逐轮核对三组条件一致。角色包 hash 为 84703bd217b5ce6239b5e50a44abc2d282acc31ebc4bda13f4a81a0a30998d3d，语料 hash 为 de1542c6bc10f17f994ce0c59f950ed0e2cfad3bc34e4ccf0ebc28f88393d0b9。

| 批次 | 真实请求 | 限额与结果 |
| --- | --- | --- |
| E2 小样本 | 4 | 最多 6 请求/150000 预留 tokens；闲聊、研究、真实工具均完成 |
| 512 输出完整对比 | 99 | 最多 110 请求/2100000 预留 tokens；91/96 轮完成，5 个 incomplete_response |
| 2048 输出完整对比 | 99 | 最多 105 请求/2200000 预留 tokens；96/96 轮完成 |
| Episode/State smoke | 4 | 最多 4 请求/200000 预留 tokens；2 Episode + 2 State，全部检查通过 |

总共 206 次 API 请求，无付费 Judge。512 批 E0 在反驳/严肃专业场景未完成，E1 在研究/严肃专业场景未完成，E2 在严肃专业场景未完成。旧 Trace 没有保存错误的细分停止原因与 usage，不能逐项断言 max_output_tokens；现象与较小总输出额度相符，提高统一上限后的独立复测全部完成。该限制属于评测设置，应用正常输出配置未被改写，也没有运行时自动加预算或自动重试。

## 2048 输出对比结果

| 变体 | 完成轮数 | API 输入 tokens | API 输出 tokens | 其中 reasoning | 轮延迟中位数 |
| --- | --- | --- | --- | --- | --- |
| E0 Profile | 32/32 | 91874 | 10130 | 5433 | 2.497 秒 |
| E1 + Behavior/Examples | 32/32 | 141143 | 9221 | 4964 | 2.428 秒 |
| E2 + 受控 State | 32/32 | 143439 | 9939 | 5625 | 2.508 秒 |

三组输入 376456、输出 29290 tokens，其中 reasoning 16022，reasoning 已在输出中，不重复相加。这是 Provider usage，不是保守估算器预留的 2008466 tokens，也不是金额。E1 输入约比 E0 多 53.6%，E2 比 E1 多 1.6%。一次顺序试验的延迟不能证明性能差异，缓存、时序和服务波动未控制。

包括小样本、旧批次和 State 在内，已记录 usage 下界是输入 762015、输出 56187 tokens（其中 reasoning 31194）；五次旧未完成请求的 usage 缺失，总量不能当作完整账单。没有可靠计价/缓存细分，未估算金额，汇总中的未估价不表示零费用。

## 七维效果审阅

本节为编码助手对三组各 12 个单轮案例和漂移第 1/10/20 轮的抽查，共 45 条回答；另看两次真实 State 变化。不是独立人工盲评，也没有使用付费 LLM Judge；没有把关键词命中转成语义分数。JSON 中 semantic_scores 保持 null。

| 维度 | 可观察表现 | 限制或问题 |
| --- | --- | --- |
| Character Fidelity | 倾向拆解含糊结论、寻找异常和可区分解释的观测；E1/E2 闲聊有较明确的证据关注点 | E0 已有同类倾向，独特人格有时仍接近严肃通用助手；不能归因于单个模块 |
| Situational Coherence | 专业任务保持专业，挫折场景能区分拒稿与个人价值，允许暂缓行动 | 普通整理桌面也被解读成动机/拖延的二分猜测；关心容易过快变成分析任务 |
| Behavioral Signature Consistency | 研究反例、无证据优势和自身错误均触发核验/修正倾向 | 简单加法被扩展成较长的类型和边界审查，怀疑倾向有表演化、低收益的一面 |
| State Continuity | 受控 E2 状态注入不妨碍任务；独立真实 State 随反例/关闭改变 | 对比中的 E2 是夹具，不测自由情绪更新效果；未证明动态状态提高了自然度 |
| Relationship Continuity | 能保留未上线的更正，拒绝把贝克街当作共同线下经历 | 某些回答把更正扩张到不相关的已确认事实，或混淆合成记录与实际验证的层级；熟悉感仍弱 |
| Task Reliability | 三组真实 add_numbers 均成功且引用实际结果；没有虚构感官或工具成功 | 纠错回答仍有把相关性当作因果必要线索的过强表述；类型风险举例有时不严谨。完成状态不代表知识全对 |
| Cost & Latency | 没有逐轮第二个 Appraisal；本批延迟约 2.4–2.5 秒中位数 | 行为签名和示例有明显输入开销；一次抽样不支持成本收益排序 |

20 轮中仍能保持未上线与无实际执行证据的边界，但这是 teacher-forced 测试：候选答案不进入下一轮，输入还明确带有合成占位特征。它验证固定暴露下的表现，不能证明自然长对话的自由漂移、真实共同叙事或日常陪伴质量。反驳案例没有给出被反驳的原推断，模型要求补充前文是合理反应，不能机械判作不肯认错。

## 真实 Episode → State

在全新隔离 SQLite/Memory、固定合成时钟与本角色 Host Run 标签下，输入两个可追溯事件：用户提出最小反例并要求先核验；半小时后确认是数据列交换造成的伪反例，明确结束话题。EpisodeOrganizer 和 StateService 都使用真实 Provider，状态经实际引文、来源和版本校验后发布。

第一次状态进入谨慎关注，Focus 指向核验反例；第二次接受撤回，转为中性并清空 Focus，revision 从 1 到 2。重复 run 不发请求；推进时钟至过期后 active() 为空，不增加请求；Memory 文件不变；四次请求均空工具，未产生主动聊天。State 两次实际用量为输入 6991、输出 950 tokens（reasoning 616）；Episode 两次输入 2429、输出 1917（reasoning 1385），二者分开核算。

此 smoke 主动调用 force=True，验证状态服务和证据发布，不测真实 Idle 调度等待、公平性或前台抢占；这些仍由离线 Runtime 测试验证。合成历史助手回复是固定夹具，不冒称是本轮主对话的真实生成。有效期通过固定时钟推进测试，没有等待三小时。

## 本轮修复与复现

评测新增 --output-tokens（256–8192），输出上限/超时进入参数与 condition_hash；默认仍为 512 以保留旧对照，真实语义检查推荐显式 2048。Trace 现在从未完成响应的安全诊断记 usage、状态/停止原因，finally 统一观察用量；不保存异常体或未完成正文。

同时发现工具轮的 Provider replay 可能携带推理正文。Trace 现只记录标准化可见消息，不复制 provider_data；传给实际 Provider 的重放数据保持完整。本次已有本地结果也移除了该字段，不把推理写进审阅报告或 Git。源码/角色包/语料没有为迎合结果改 Prompt。

```powershell
# 会产生真实 API 费用，均只使用合成输入：
uv run python -m personal_agent.evaluation.character --live --variant all --output-tokens 2048 --max-requests 105 --max-tokens 2200000
uv run python -m personal_agent.evaluation.character_state --live
# 默认/帮助入口不调用 API：
uv run python -m personal_agent.evaluation.character --help
uv run python -m personal_agent.evaluation.character_state --help
```

State smoke 从本轮隔离脚本整理为 evaluation/character_state.py 的等价独立入口，最多四次真实请求；未为入口整理而重复收费。--live 守卫、失败 usage、输出参数配对和推理重放脱敏均有离线回归测试。

最终离线 `uv run pytest -q --basetemp=.heybuddy/p3-live-final-verified` 为 671 passed、2 skipped（127.96 秒）。Ruff check、format --check、uv build --offline、普通 CLI 与两个评测入口的帮助检查通过；wheel 包含四份角色 Markdown 和 State smoke 入口，sdist 不含 .env/.heybuddy。源码包与冻结语料逐字核对基准未改。网络保护保持，pytest 没有真实 API 调用。提交哈希见交付回复。

## 下一步

优先修正与评估更正的作用范围、缺少证据时的措辞和简单任务的分析节制；对照应保持独立 Holdout，不在本批分数上调整。关系体验需追加不带合成提示词的真实结构化夹具和自由生成 20 轮测试，比较用户偏好遵守、事实边界及过度追问。若试验 Selective Appraisal，应先证明少量复杂场景的收益足以覆盖额外请求；当前证据不足以增加在线认知调用。
