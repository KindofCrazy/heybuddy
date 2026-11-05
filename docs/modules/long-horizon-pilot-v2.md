# v2 三场景真实模型 Pilot

## 职责与架构组成

`evaluation/long_horizon/pilot.py` 在 v2 离线接线通过后提供独立真实模型入口，固定开发场景 LH019、LH021、LH022，按 baseline、state、identity、fewshot 顺序各重放一次，共 12 Trial、216 Turn。基准与候选在同一冻结语料、时间、回复和维护条件下配对，场景之间和组之间隔离。Holdout 不进入执行路径。生产 Router 的默认策略不因实验切换。

`pilot_protocol.json` 随包分发，保存五份数据 Hash、场景/组顺序、维护周期和首次真实调用前冻结的九个摘要检查点。Gold、审阅问题及答案不交给 Router 或 Organizer。`PilotBundle` 保存完整语料、协议、Prompt 策略和轨迹，支持不依赖当前语料文件的重新评分；报告保留代码/Git/配置/协议 Hash。

`PilotConfig` 继承共用请求元数据，但使用独立 routing-pilot-v2 profile，限制已核实的 deepseek-flash 与 api.deepseek.com，所有阶段显式 reasoning_effort=none。原 smoke/v1 类型的字段与上限不改变。`LiveConfig.request_limits()` 默认仍返回旧输入/输出上限；仅 Pilot 根据 stage 区分 routing 与 episode/thread。

## 代码实际运行路径

1. CLI `--live --variant model-assisted --budget-profile routing-pilot-v2` → `run_live()`。正常配置加载器读取现有本地配置，不输出密钥；创建 SDK client 前验证模型/端点、冻结数据 Hash、开发集及无单场景覆盖。SDK max_retries=0、请求超时 30 秒；真实 Router 原有 10 秒选择超时继续有效，Organizer 保留自身 60 秒上限。
2. `evaluate_pilot()` 在首个请求前发布私有 pilot-metadata.json，记录授权范围、参数、Prompt、代码和协议来源。创建一个 `MeteredProvider` 和一个累计 `Budget`，整个实验共用，组/场景/阶段切换不重置。
3. 每 Trial `PilotAdapter` → `PolicyProvider` → 真实 Router。Adapter 在选择前设置 arm/scenario_id/turn_id/stage=routing；Provider 对实际请求做 estimate_request 和预算预留，保存 running 回执后才发出网络请求。收到结果时分别核对 Usage、工具禁用和单次上限，保存完成或失败回执。模型选择解析失败仍按真实 Router 的 selection_unavailable 记录，不能当作模型成功选择。
4. state/identity/fewshot 组在每六个完成 Turn 后使用 `PilotMaintenance` → 真实 EpisodeOrganizer/ThreadOrganizer/repair。`StageProvider` 根据受控 Organizer 请求结构标记 episode/thread，调用同一个 MeteredProvider；收到未知 Usage 或超限响应立即抛错，不允许未知结果继续发布。生产整理器的来源、固定归属和 CAS 校验继续生效。
5. 维护使用 interval=6、page_runs=8、revision_seconds=1、idle_seconds=1、输入策略 32768、输出 4096，合成完成时间加两秒模拟空闲；ContextBudget 使用 Host 配置的真实窗口，不能替换为猜测窗口。四组维护时点一致，baseline 无维护。与此前 interval=3 的离线对照是不同实验配置，不混算。
6. 每次调用前后及 Trial 完成后原子保存 pilot-progress.json。任何 API、未知 Usage、预算、维护失败、取消或超时均终止整个实验，不自动重试、提高限额或续跑工具。失败状态保留已完成 Trial 名单和阶段用量；不完整实验不能生成完整四组能力汇总或通过成功重算入口。
7. 全部完成后 `summarize()` 核对 12 Trial 和 Turn/维护覆盖、路由逐 Turn 调用、维护逐阶段记账、单次选项、累计预留和实际 Usage，再生成真实模型 Pairwise、碎片化、污染、澄清、coverage 和分阶段用量。`recompute_pilot()` 先核对状态与 Trace Hash，再从冻结 Bundle 无网络重新评分。

## 预算与边界

总上限人民币 10 元、400 请求、2000000 累计预留 tokens、3600 秒。路由输入估算最多 8192、输出 1024；Episode/Thread 输入估算最多 32768、输出 4096；每请求还核对 Host 窗口与 1024 安全余量。真实 Organizer 同时执行自己的窗口限制。

`Budget` 累计预留每次估算输入和完整输出额度，不在调用后退回差额；实际 Usage 另行统计，reasoning 已包含在 output_tokens，不重复计费。未知 Usage、超出单次/累计用量或成本均停止。输入估算不是精确 tokenizer，单价与费用上界不是服务商账单。

2026-11-05 复核 [DeepSeek 官方价表](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)和[思考参数](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)，按高峰未缓存输入 2 元/百万、输出 8 元/百万作为较高单价估算。首轮不采用默认 high 思考，避免重现旧 v1 推理占满路由输出的 incomplete_response。此处为独立实验配置，不修改生产 Provider 默认参数。

`--pilot-preflight` 使用固定 continue/clarify/new 和提取式 Organizer，无 Settings/Key/网络。interval=6 的三条固定路径累计预留估算约为 6.56352、6.56352、7.314798 元；真实选择和 Episode 分组可能不同，不能当作必然费用上界。首轮 scope 和规则不因模型得分而修改。

## 独立的协议 JSON 输出实验

首次 strict-text Pilot 在第一个 state 场景的 T06 维护中收到带 Markdown 围栏的 ThreadRepair，严格解析失败，原实验停止并保留失败轨迹。三个 baseline 场景已完成，共 19 次真实请求，输入 16140、输出 1569，reasoning 为 0；实际用量费用上界 0.044832 元，累计预留成本为 0.37589 元。未得到完整 state/identity/fewshot 结果，不计算四组整体能力结论。

无正文回执见 [首次失败记录](../../evals/results/long-horizon-v2-live-pilot-20261105.json)，原始私有 metadata/progress/status 保留在 `.heybuddy/evaluations/lh-v2-live-pilot-20261105/`。第一次维护的部分摘要提案已做有限来源核对，但尚无完整维护快照，不能作为摘要准确率。

经核对服务商支持后，新增显式 `routing-pilot-v2-json`，用 `JsonPilotConfig` 和独立 Trace 版本 live-pilot-v2-json-1，所有组由专用 `OpenAIProvider(json_mode=True)` 发送 text.format=json_object。Prompt、语料、Gold、维护时点和生产严格解析不改变；不把去围栏当成原实验成功，不执行 SDK 重试或恢复旧 Run。四组从各自新隔离环境重放，不把两次条件混算。

JSON profile 必须提供 `--prior-attempt`，`prior_attempt()` 先核对原失败 metadata/progress/status 的冻结范围、完整 Usage、调用和预留记账，密封其 Hash，按原累计预留额扣除预算。JsonPilotConfig 再校验两次预留合计不超过 10 元、400 请求、2000000 tokens；本次剩余上限为 9.62411 元、381 请求、1907287 tokens，不按更便宜的实际费用退回差额。旧失败回执不改写，独立结果标明 JSON 条件和前次来源。空输出、非法 Schema、预算和 API 错误仍停止，不自动补救。

```powershell
uv run python -m personal_agent.evaluation.long_horizon --dataset evals/long_horizon_v2 --live --variant model-assisted --budget-profile routing-pilot-v2-json --prior-attempt .heybuddy/evaluations/lh-v2-live-pilot-20261105 --input-price-cny 2 --output-price-cny 8 --price-source https://api-docs.deepseek.com/zh-cn/quick_start/pricing/ --output .heybuddy/evaluations/lh-v2-live-pilot-json-20261105
```

## 摘要语义审阅

九个检查点为三个场景各第 6/12/18 Turn 后。审阅者是开发 Agent，不是独立人类或另一个付费评审模型；公开审阅记录必须注明此限制。baseline 没有状态，标为不适用；其他组检查所有实际碎片，不能只看首次 Thread 或根据未来 Gold 给 Router 修复历史。

审阅以当时已完成的原始用户证据为准，分别记录支持的事实、遗漏、被新证据替换但仍残留的旧值、无来源内容和无法评价。来源混入错误 Thread 时，要区分路由污染、整理器错误关联与凭空捏造；摘要准确复述混合输入并不等于整理器自己跨主题关联。冻结助手回复、过去未接受的建议和未来事实不能升级为用户事实。不通过关键词命中给出伪装成语义准确率的分数，报告 state_semantic_accuracy 保留 null，人工式审阅单独附来源与结论。

## 阅读与验证

```powershell
uv run python -m personal_agent.evaluation.long_horizon --dataset evals/long_horizon_v2 --pilot-preflight --output .heybuddy/evaluations/lh-v2-pilot-plan-example
uv run python -m personal_agent.evaluation.long_horizon --dataset evals/long_horizon_v2 --live --variant model-assisted --budget-profile routing-pilot-v2 --max-cost-cny 10 --input-price-cny 2 --output-price-cny 8 --price-source https://api-docs.deepseek.com/zh-cn/quick_start/pricing/ --output .heybuddy/evaluations/lh-v2-pilot-live-example
uv run python -m personal_agent.evaluation.long_horizon --report .heybuddy/evaluations/lh-v2-pilot-live-example
uv run pytest tests/test_long_horizon_pilot.py tests/test_long_horizon_live.py tests/test_long_horizon_v2.py
```

输出目录必须全新且满足既有隐私规则，完整消息/摘要/数据库不入 Git，只提交无正文汇总。pytest 始终保留 conftest 的禁止外网与无真实凭据保护。测试使用 Fake Provider/MockTransport，验证四组完整性、真实维护、累计共享记账、超预算在发出请求前拒绝、未知维护 Usage 不发布/不继续、协议资源不依赖 cwd、范围/模型/端点限制、完整性和篡改后重算拒绝。
