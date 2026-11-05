# Long-Horizon 真实模型评测方案与预算

本文件保留两阶段方案与预算依据。2026-11-05 用户已授权先验证接线 smoke，沿用现有配置，第一轮最高人民币 10 元、最多 5 次模型调用；第二轮正式语料仍待后续建设。第一轮实现与真实执行路径见[模块文档](modules/long-horizon-evaluation.md#有界真实模型-smoke-的实际路径)。下文“待接入”清单保留最初方案，当前状态以模块文档为准；不读取生产会话或 Memory。

## 先明确评测对象

第一轮评估生产 `ThreadRouter(model_selection=True)` 的混合路由：规则先选，只有未决输入才调用 Provider。保持生产 Router、Prompt、Agent Loop 和 Learning 策略不变；助手回复继续冻结，Memory Learning、主模型回答和后台整理均不运行。运行类型必须标记为 Real Model Evaluation，不能与现有 Offline Contract Test 混算。

2026-11-05 使用真实 Router 与固定返回 clarify 的模拟 Provider 进行预算 dry run，结果如下。输入量来自 `core.budget.estimate_request()`，是本项目估算，不是实际 tokenizer 或计费 Usage。

| 场景 | Turn 数 | 模型分支调用数 | 估算输入 tokens | 单次输出上限 |
| --- | ---: | ---: | ---: | ---: |
| TR001 | 3 | 0 | 无调用 | 无调用 |
| TR002 | 3 | 0 | 无调用 | 无调用 |
| TR003 | 3 | 0 | 无调用 | 无调用 |
| TR004 | 3 | 1 | 1603 | 1024 |

因此，当前四个 Fixture 适合作为模型接线 smoke；即使混合 Router 总分很高，也不能据此证明模型理解跨话题返回。必须分别报告全部路由决策、规则决策、真实模型决策及各自 support。ML001 不纳入本轮真实路由评分。

## 分两阶段执行

| 项目 | 第一轮：接线 smoke | 第二轮：正式路由开发集，另行批准 |
| --- | --- | --- |
| 数据 | 冻结 TR001–TR004 | 新增人工固定的 10 个 Scenario，每个 6 Turn |
| 实验 | rule 基线、model-assisted 各 5 次独立 Streaming | 两个 Variant 各 3 次独立 Streaming |
| 每 Variant Turn 数 | 60 | 180 |
| 付费请求上限 | 5 | 150 |
| 每请求估算输入上限 | 2048 tokens | 4096 tokens |
| 每请求输出上限 | 沿用生产 1024 tokens | 沿用生产 1024 tokens |
| 全实验预留 token 上限 | 20000 | 800000 |
| 请求策略 | 串行、无重试，原 Router 10 秒超时 | 相同 |
| 实验总时限 | 120 秒 | 1800 秒 |

第一轮预计只有 TR004 每次重复触发一次模型，共 5 次；其他场景仍为规则决策。五次相同输入只能观察重复稳定性，不构成五个独立语义样本。模拟 Provider 的路由输出仅用于调用预算分析，不能作为真实模型预测。

第二轮请求上限按每 Scenario 第一条无候选、其后五条均可能调用模型计算：10×5×3=150。全局输入估算及输出预留最多 150×(4096+1024)=768000 tokens，800000 为实验预算上限；运行时仍先检查单请求和全局预算。正式语料需要包含无显式“另外”的新任务、相似项目不同身份、无标题复述的旧话题返回、信息不足的指代和跨天延续。先审阅并冻结 Gold 与独立 Holdout，再看模型输出；不得根据分数修改 Gold。Holdout 的调用预算单独批准，不包含在上表中。

两阶段均按当前流式状态独立建库；不同重复不共享 Thread/Memory。当前没有 Oracle-history 实现，不能通过改路由表或强迫每步调用模型冒充生产混合 Router 结果。未来若做固定标准历史的单步诊断，必须另设实验类型并单独报告。

## 费用如何计算

模型、服务商、端点尚未指定，因此目前不给出声称适用于真实账单的金额。设经核实的输入/输出价格分别为 P_in、P_out（每百万 tokens，同一币种），则：

- 第一轮按 dry run 量估算：`(8015 × P_in + 5120 × P_out) / 1000000`。
- 第一轮按单次估算输入上限预留：`(10240 × P_in + 5120 × P_out) / 1000000`。
- 第二轮按全部请求触发预留：`(614400 × P_in + 153600 × P_out) / 1000000`。

这些是估算预算，实际输出通常低于上限；输入估算也可能偏离服务商计数，不能当作硬账单封顶。必须记录实际 usage；reasoning 若已包含于 output，不重复计费。缓存折扣只在可靠 usage 和价格规则支持时计入，否则按未缓存价保守估算。未返回 usage 标为 unknown，不填零；无法确认计费量时停止后续付费请求，保留已发生费用的不确定性。

接入时复用 `evaluation.runner.Budget.reserve/observe`，在包装 Provider 中扩展 usage 完整性、实际费用复核和停止条件，不能直接沿用其 unknown 累计值作为零。货币上限必须绑定已核实的价格；请求前估算拦截与请求后实际复核都要有，但仍不承诺服务商侧硬封顶。正式金额需在模型确定后按官方价或实际端点合同价补齐；不把示例价格当作现价。

## Evaluation Adapter 的接入改动

仅修改 `evaluation/long_horizon/`、相关测试及文档，尽量复用原 Provider、SQLiteStore、RoutingRepository 和 ThreadRouter：

1. Config 明确区分 rule 与 model-assisted，保存 model、endpoint 的安全标识、显式 context window、重复次数、预算与计价参数；Gold 与生产 Memory 均不得进入模型请求。冻结 Prompt 内容/Hash及评测代码版本，保存 Git dirty 标记。
2. Replay 由宿主选择 Adapter，生命周期仍为 begin_run → 真实 route → append 冻结回复 → finish_run。模型辅助 Adapter 只开启 model_selection，不重写路由策略；两个 Variant 使用相同冻结历史输入。
3. Provider 包装器在每次真正 generate 前预留共享预算，记录实际调用次数、usage、耗时、安全错误类型和私有请求/返回；空工具列表，SDK max_retries=0，不记录原始 reasoning 或密钥。
4. Router 会把超时、非法选择和 ProviderError 转成 selection_unavailable。Adapter 必须保留其原始调用错误诊断，不能把这种回退计为模型成功；预算耗尽、取消及 usage 未知须停止后续调用，并将实验标为不完整。请求被吞入 Router 回退后也要检查包装器的停止标记。
5. CLI 仅在明确的 `--live`、支持的 Variant、已验证模型/价格/限额同时满足时创建真实客户端。默认离线路径保持无需 Key、无需网络；当前这些 live 参数尚未实现。
6. 私有轨迹只存 `.heybuddy/evaluations/` 等安全忽略目录。Public Summary 分离规则/模型 support、模型成功/回退/歧义、Pairwise、Clarification Rate、调用量与 usage 完整性。误判是正常评分结果；实验完整性与预测正确率分开。
7. 离线测试先覆盖预算阻断、缺失 usage、超时与错误回退、取消、Gold 隔离、无 live 拒绝、无自动重试及报告重算。真实调用不加入 pytest；离线检查通过后再实施已批准的 smoke。

## 执行前需要补齐的信息

需要明确模型标识、服务商/端点、显式 context window，以及第一轮允许的最高金额和币种；价格需按该端点核实。密钥只通过现有环境配置获得，不在聊天、文档或示例中提供。本方案不扩大到 Memory Learning、主模型长期 QA、完整 LongMemEval 或后台任务评测。
