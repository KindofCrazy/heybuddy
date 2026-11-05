# Thread Routing Benchmark v1 — synthetic draft

本目录是 12 个逐条编写的合成场景，共 216 Turn。它用于比 Phase 1 更接近日常聊天的路由实验，**不是采集的真实用户数据，也不是经人工独立审定的 Gold**。作者为本次开发 Agent，标签和说明均显式写出，没有通过运行时模型自动标注，没有读取生产 SQLite/Memory/QQ 历史。`protocol.json` 固定 `human_reviewed=false`；语义标签仍需后续人工复核，不能把 draft 分数当作正式模型能力结论。

## 数据与冻结

- `corpus.json`：输入、冻结助手回复、时间、来源、Conversation Key；不含话题标签、允许动作或证据标签。
- `gold.json`：每 Turn 的稳定逻辑话题、允许动作、ambiguity 与历史证据 ID；与实际 Router 输入分离。
- `splits.json`：按完整 Scenario 划分 dev 8 个、holdout 4 个；没有从同一场景拆分 Turn。
- `protocol.json`：评分版本、作者/审阅状态、长度/划分约束和每个场景的标注说明，不交给 Router/Provider。
- `config.json`：默认 rule/streaming/network=false；`manifest.json` 冻结上述五份文件的规范化 JSON SHA-256。

每场景 18 Turn，跨 3 个日期。来源均为合成 CLI，每场景独立数据库和 Memory；冻结回复为对当前事项的简短自然回应，发生模糊指代时的标准回复可追问。两个 Variant 都使用相同标准回复，所以它测的是冻结回复条件下的流式路由，不是完整生成式对话效果，也不是 Oracle-history。

| Split | ID | 场景 | 主要判别点 |
| --- | --- | --- | --- |
| dev | LH001 | 花店手机端与网页端 | 相似项目独立、隐式返回、清单歧义 |
| dev | LH002 | 毕业论文与面试讲稿 | 相同截止日、跨天返回、稿子歧义 |
| dev | LH003 | 自住房与出租房维修 | 对象区分、报价更正、费用指代 |
| dev | LH004 | 家庭旅行与同城出差 | 地点相同但任务独立、酒店指代 |
| dev | LH005 | 备菜与徒步穿插闲聊 | 闲聊分段、感谢后续接、隐式返回 |
| dev | LH006 | 两家公司的应聘流程 | 同岗位不同流程、材料接收方 |
| dev | LH007 | 评审材料与自行车保养 | 改期仍属原任务、风险撤回、误碎片化 |
| dev | LH008 | 咖啡机与办公室打印机 | 可解指代与真歧义的对照、过度澄清 |
| holdout | LH009 | 露天音乐节与学校汇演 | 同设备不同场地/规模 |
| holdout | LH010 | 两个志愿小组 | 同地点不同排班、局部改期 |
| holdout | LH011 | 助教出题与个人备考 | 同教材不同目标、相邻明确指代 |
| holdout | LH012 | 读书会与登记工具 | 活动/软件边界、清单歧义、闲聊后恢复 |

Holdout 已按场景封存。本轮只做结构、时间、引用和 Hash 校验，不执行其路由、评分或模型预算路径；开发结果不能用于改 Holdout 标签。它仍是同一作者设计的本地 Holdout，不宣称盲测、第三方独立性或不可访问。后续若发现语义标注问题，需要说明理由、升版本并重新冻结，不能静默追分。

## Gold 的解释与复核重点

topic_a/topic_b/social 是场景内的逻辑标识，不是数据库 UUID，也不出现在 Router 输入中。同一任务的改期、范围变更、更正继续归原话题；两个项目即使术语、地点、岗位相似，也保持独立。真正无法唯一定位的指代标为 ambiguity，允许 clarify；紧邻且唯一可解的指代应允许 continue。澄清后的明确恢复可同时允许 continue/select，避免强行等同宿主内部动作。

`relevant_source_turn_ids` 固定引用该话题首条、最近同话题以及最近其他话题的已有证据，去重并严格指向过去；模糊项引用两个候选的近期证据。它们是评分用关系锚点，不注入模型。边关系正确是“同 Gold 话题应同实际 Thread、异 Gold 话题应异实际 Thread”。首条无关系证据不计为已正确分类；预测歧义、Gold 歧义和无可用关系证据的 Turn 单独保留 coverage，并打断连续错误统计。

复核时尤其检查：场景中的项目边界是否合理、模糊输入是否其实可由最近上下文唯一解释、标准回复是否泄露未来事实、局部更正是否误变新任务，以及允许动作是否覆盖合理路由。当前动作准确率仍未评分，不把 reason 强行转换成 action。

## 运行

```powershell
uv run python -m personal_agent.evaluation.long_horizon --dataset evals/long_horizon_v1 --list
uv run python -m personal_agent.evaluation.long_horizon --dataset evals/long_horizon_v1 --variant rule --split dev
uv run python -m personal_agent.evaluation.long_horizon --dataset evals/long_horizon_v1 --preflight --split dev
```

`--preflight` 用真实 ThreadRouter 和三个固定模拟选择 continue/clarify/new 探测调用路径，无网络、无 Settings/Key，无模型 Usage，也不输出模型预测分数。实际模型路径可不同，不能把模拟结果当作真实模型能力。

默认 Live 入口仍限制为上一轮 smoke 的 5 次请求/20000 tokens；本轮没有放宽，也没有运行付费评测。完整 v1 Live 需要后续显式授权预算并接入相应实验配置。先看开发集，Holdout 留到协议/实现稳定后的单独运行。指标、离线结果与预算说明见 [v1 模块文档](../../docs/modules/long-horizon-benchmark-v1.md)。
