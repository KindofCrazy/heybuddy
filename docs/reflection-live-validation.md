# V0.7.1 Reflection 真实模型诊断与复测

日期：2026-10-27。模型：deepseek-flash；端点：api.deepseek.com。仅发送专门构造的演示历史/Memory，未使用真实私人会话；生成计划后不 Apply。真实调用为用户明确要求的手动验证，不进入自动化测试。

## 配置及修复

按照[官方模型元数据](https://api-docs.deepseek.com/api/list-models/)把本地 AGENT_CONTEXT_WINDOW 显式设为 1048576（总输入/输出窗口）。该私有 .env 保持 Git 忽略；仓库示例仍要求用户为各自模型显式配置，不引入模型名猜测或启动自动发现。

旧 Reflection 把 output_tokens=4096 同时当作 API 总输出 tokens 和可见 JSON 字节数。推理模型的[总输出包含 reasoning](https://api-docs.deepseek.com/api/create-response/)，因此同一数值混用两种单位会造成错误的限制。修复后 API 总输出默认 16384 tokens，response_max_bytes 独立限制可见 JSON 为 32768 UTF-8 字节。每阶段输入仍扣除总输出和安全余量；没有因窗口扩大而全量加载历史。

Provider 新增安全诊断：固定状态、白名单停止原因与数值 input/output/reasoning 用量；ReflectionError 保留 extraction/selection/consolidation 阶段，CLI 展示这些字段。未完成正文、reasoning 文本和 SDK 异常体不出现在错误信息里；失败仍拒绝保存计划、执行工具或自动重试。正常响应的原始 Provider 数据重放契约不变。

提取/整合提示同时强调：一次性假设或虚构示例的免责声明不能被扩张成永久用户规则；没有需要纠正的既有错误事实时，应省略或拒绝这类候选。

## 旧额度对照诊断

第一次 V0.7 验证的整合阶段报 incomplete_response，但旧 adapter 丢失 incomplete_details，不能事后断言该次失败的具体服务端原因。本次只重放该阶段一次，仍使用 4096 总输出 tokens；返回 completed，用量为 input=2176、output=3596，其中 reasoning=3086，可见 JSON 为 1995 字节，耗时 22.47 秒。

这次对照没有复现失败；它说明推理占用总输出的绝大部分、原额度余量有限，也说明模型运行存在波动。不能把此次 completed 当成已经证明上一次必然因 max_output_tokens 中断。补齐诊断后，未来遇到该停止原因可以明确区分。

## 修复后完整复测

使用与失败验证相同的 7 个 completed Run（14 条消息）、已有项目主题与空 human.md，按正常 ReflectionService 三阶段运行。

| 阶段 | 耗时（秒） | input tokens | output tokens（含推理） | reasoning tokens | 可见 JSON 字节 |
| --- | --- | --- | --- | --- | --- |
| Extraction | 17.05 | 1841 | 2981 | 2329 | 2372 |
| Selection | 2.34 | 1159 | 250 | 231 | 55 |
| Consolidation | 6.35 | 1813 | 1100 | 792 | 1254 |
| 合计 | 25.74 | 4813 | 4331 | 3352 | 3681 |

提取 3 个候选，程序验证原始来源/引文通过，选择 projects/MEMORY.md 与 projects/heybuddy.md，成功保存未批准的审阅计划：

| 样本 | 最终动作 | 人工质量检查 |
| --- | --- | --- |
| 两次明确确认“先结论、后必要依据、简洁” | 局部更新 human.md | 合并成一条稳定偏好，同时保留两次用户证据；没有生成重复规则 |
| 已有自建 asyncio Harness、不引入 LangGraph、SQLite 会话持久化 | no_op | 识别已有主题已覆盖，不复制到新文件或 Core |
| 合成样本中明确把 JSON 实验日志改为 SQLite experiment_events | 局部更新 projects/heybuddy.md | 原位替换旧 JSON 结论，保留事务/参数化查询理由及 Markdown Memory 边界，未修改无关架构 |
| 尚未确认的强化学习假设 | 无最终候选 | 未误写成长期研究方向或额外禁令 |
| 虚构示例不代表用户所在地 | 无最终候选 | 未推断用户住北京，也未积累一次性禁令 |
| 临时加法问题 | 无最终候选 | 没有长期保存 |

experiment_events 是验证材料中的合成决策，不是本次为 HeyBuddy 新增的数据库功能。

Memory 全部 Markdown 逐字节未变，原始消息仍为 14 条，Reflection 进度仍为 0，三个条目都为 pending，未授予普通聊天任何 Memory 工具权限。扩大总输出上限并不要求模型用满上限；本次实际只用到表中额度。

## 验证边界

这是单个合成场景的一次完整真实模型复测，表明这一组提案可用，不证明所有历史/模型运行都正确。近似去重、事实价值与引文的语义关系仍需用户审阅；遇到更复杂材料或达到独立额度仍会明确停止。后台反思、自动 Apply 和自动增额重试不在本修复范围内。

离线回归另验证正常 JSON 大于旧字节限制仍可接受、新字节上限继续生效、未完成原因/usage 安全传递、未知原因归一化、缺失 reasoning details 兼容，以及失败无计划/无记忆写入/无重试。

## 离线与安装包验证结果

完整 `uv run pytest`：398 passed、1 skipped（Windows Symlink 创建权限）；Ruff 检查、格式检查、uv build、两个 --help 入口通过。自动测试的网络禁用保护保持不变，没有把手动付费模型验证加入 pytest。

构建的 0.7.1 wheel 在两个独立进程执行 /reflect status、/context status、空历史 /reflect 后退出：确认 1048576 窗口、新输出/JSON 独立默认值、schema 4 和相同会话恢复，未创建 Run 或触发模型调用。私人 .env、临时数据库、真实验证原始输出及构建产物均保持 Git 忽略。
