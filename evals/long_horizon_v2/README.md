# Thread Routing Benchmark v2 — synthetic draft

30 个合成场景，共 540 Turn，每场景 18 Turn。保留 v1 的 12 场景及原标签，新增 18 场景，dev 21 个/378 Turn、holdout 9 个/162 Turn。原 Holdout LH009–LH012 保持封存，新 Holdout LH026–LH030 不参与开发路由实验。

数据由开发 Agent 逐条显式编写，不采集生产聊天、数据库或 Memory，不用运行时模型自动生成标签。Gold 尚未经独立人工复核，protocol 固定 human_reviewed=false；这是待复核的合成草案，不是正式真实用户 Benchmark。所有输入、标签、划分与协议在首次 v2 评分前冻结。

Corpus 与 Gold 分离；manifest 记录 corpus/gold/splits/config/protocol 的规范化 JSON SHA-256。Router、摘要维护和 Prompt 都不读取 Gold 或未来 Turn。冻结助手回复用于固定对话历史，不是被测模型生成的回答；新的状态维护使用实际预测 Thread 归属，不能用 Gold 修正错误。

| Split | ID | 新增场景与判别重点 |
| --- | --- | --- |
| dev | LH001–LH008 | 原 v1 八场景，逐字保留 |
| dev | LH013 | 客户门户与内部工单：相似登录技术、独立站点 |
| dev | LH014 | 三家门店招牌：三个相似交付的身份 |
| dev | LH015 | 运费与培训报销：同名附件、不同费用对象 |
| dev | LH016 | 字幕与海报：同一客户、独立交付 |
| dev | LH017 | 离心机检修与样本登记：同实验室、独立任务 |
| dev | LH018 | 台灯维修、种植和闲聊：分段与恢复 |
| dev | LH019 | 服务部署与照片筛选：部署子步骤不拆分 |
| dev | LH020 | 聚餐与演出票：跨天、改期和取消 |
| dev | LH021 | 持续作品集任务：唯一指代、避免过度澄清 |
| dev | LH022 | 两份合同：四组歧义与明确恢复 |
| dev | LH023 | 运营、销售和客服报告：三条相似任务 |
| dev | LH024 | 工作室搬迁与摄影课：多次更正、局部改期 |
| dev | LH025 | S2 与 S2Plus：相近编号、不同测试目标 |
| holdout | LH009–LH012 | 原 v1 四场景，标签与划分保留 |
| holdout | LH026–LH030 | 亲属预约、退货/新订单、手表维修、房间布局、三个学习目标 |

话题 ID、允许动作与历史关系只供评分器使用。语义边界以稳定实体、目标和交付为准；改期、更正、补材料和子步骤一般延续原任务，相似术语不自动合并。模糊指代与紧邻唯一可解指代分别标注。人工修正需说明原因、升版并重新冻结，不按开发分数追改标签。

```powershell
uv run python -m personal_agent.evaluation.long_horizon --dataset evals/long_horizon_v2 --variant rule --split dev
uv run python -m personal_agent.evaluation.long_horizon --dataset evals/long_horizon_v2 --ablation all --split dev
```

第二条只用固定 Provider 验证真实 Episode→Thread→repair 链路和状态/Prompt/弱规则对照接线，无 API 调用，也不计算脚本预测的模型质量。语义摘要、真实模型效果与恢复问答尚未评测。实现、指标、限制和结果解读见 [v2 模块文档](../../docs/modules/long-horizon-benchmark-v2.md)。
