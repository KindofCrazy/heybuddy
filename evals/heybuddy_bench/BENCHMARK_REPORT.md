# Benchmark Review + Evaluation Report

日期：2026-11-07。数据版本：v0.2.0，150 个独立快照，三个 Track 各 50 题。审核者为编码 Agent，独立人工审核尚未完成。完整 HeyBuddy 的真实模型比较未执行；本文严格分开实际 Baseline、生产链路离线验证和待执行项。

## Review 结论与修正

原始 105 条自建题有明确输入/标签分离和较好的完成/取消反例，适合工程回归，但还不能直接作为生产评测。没有发现必须重构生产架构的问题；本次只增加独立评测模块及数据修订。

| 问题 | 修正与影响 |
|---|---|
| 新建 Memory 文件名如 `u01-reading.md` 无法由输入唯一推断 | 新建题比较合法主题区域，既有文件仍精确比较；实际文件名和 diff 全部保留 |
| Extraction 返回空集合时没有原生 reject 标签 | 保留严格动作分，同时为临时/假设题标注 reject/no_op；冲突题允许 review/no_op，另验证没有写入 |
| 旧评分只统计提交的预测，缺题可提高分数；不处理 acceptable actions | 缺题/错误纳入全体分母，重复与未知 ID 拒绝；严格和可接受动作分分别报告 |
| `chat` 被误解为不建 Thread | 使用真实绑定与 Router reason；保留“日常交流”新建和 clarify 底层绑定信息 |
| 同模板重复、候选顺序和 A/B 后缀可形成捷径 | 改写十个恢复问题，打散候选顺序，Thread ID 改为无类别含义的 UUID；仍明确标记模板相关性 |
| “刚才”实际相隔一周；性能模糊题没有性能上下文 | 修正近期时间与活跃时间，为两个潜在指代补齐上下文 |
| Proactive 的 availability/DND 不是生产 Attention 参数 | 不新增评测 Prompt；为专注题添加实际可见的合成用户原话，未消费字段写入协议说明 |
| 一些晚间题的 due_at 与用户说的 21:00 不符 | 统一为原始用户明确的时间；复查时间比较 Host 最终持久化值 |
| 七天前的行为证据在默认寿命下已到期 | 两道正例改用六天前的续约证据；夹具按最新真实来源推导 expiry，不在评测时重新延寿 |
| public 候选只有源标签，缺改编输入与证据 | 冻结 35 条原文摘录和新标签，保存版本/文件哈希/原生标签；10 个不合适配额改为自建题 |
| TriggerBench 负例仍携带正例参考意图 | 原意图仅留在 provenance，输入去除 variant/function/gold/has_answer 等标签字段 |
| 原生 TriggerBench 正例可能落在 HeyBuddy 安静时段 | 两道午夜题重标 HeyBuddy silent，原始 positive 标签保持原样 |

数据结构、时序、来源引用、update 唯一片段、路径和 SHA-256 校验通过。重建脚本输出固定 LF，Git 属性锁定换行，避免跨平台 checkout 破坏清单。原始 105 个候选仍保留供追溯。每条输入只向模型提供 `input`，不提供 category、case ID、source ID、原生 Gold 或审核说明；测试另用被污染的元数据验证隔离。

## Public Benchmark 完成情况

| Track | 公开来源 | 公开数 | 自建数 | 合计 |
|---|---|---:|---:|---:|
| Routing | TIAGE 5、IRC 3 | 8 | 42 | 50 |
| Memory | LongMemEval 8、MemoryAgentBench 4 | 12 | 38 | 50 |
| Proactive | TriggerBench 15 | 15 | 35 | 50 |
| 总计 | 五个来源 | 35 | 115 | 150 |

IRC 入群事件不是个人任务切换，部分长 QA/小说预测也不适合当前 MemoryFS。替代题覆盖同领域新任务、活跃上下文中的歧义、助手推测、已存事实、项目限定偏好、关系反馈、引用攻击和实体隔离。未强凑 45 个公开题。[来源清单、固定 revision、许可证与改编说明](public_sources/SOURCES.md) 可逐题追溯。

LongMemEval 只使用已定位的用户更新证据，MAB 只改编四个不同单跳关系；IRC/TIAGE 不再评原始回复图/话题切换，TriggerBench 加入 Host 门控。这些是 HeyBuddy 改编任务，不能称为原始 Benchmark 官方成绩。选择没有参考本次预测得分，数据未用于生产 Prompt 调参。

## 已运行 Evaluation

每 Track 只有一个简单确定性 Baseline。Routing 使用关键词重合与最近性；Memory 将用户原文追加到单一 inbox；Proactive 只检查是否在 24 小时内截止。三个 Baseline 都不调用模型，和完整 HeyBuddy 的比较是工程系统比较，不是相同 backbone 的受控消融。

| Track | Baseline 严格动作准确率 | Action Macro-F1 | 动作与目标匹配率 | 完整 HeyBuddy 真实模型 |
|---|---:|---:|---:|---|
| Routing，50 题 | 52.0% | 0.2564 | 52.0% | 未运行 |
| Memory，50 题 | 20.0% | 0.0667 | 20.0% | 未运行 |
| Proactive，50 题 | 40.0% | 0.2180 | 42.0% | 未运行 |

Proactive 可接受动作准确率为 42.0%，比严格分多一题合理 silent；Gold silent 的误打扰率为 **48.0%**，notify/propose_task 精度为 **26.9%**。每 Track 的动作与目标匹配率 Wilson 95% 描述区间分别为 Routing 38.5%–65.2%、Memory 11.2%–33.0%、Proactive 29.4%–55.8%；模板题有关联，不能把区间解释为独立真实用户泛化保证。

逐来源正确题数：Routing 自建 23/42、TIAGE 2/5、IRC 1/3；Memory 自建 9/38、LongMemEval 0/8、MAB 1/4；Proactive 自建 12/35、TriggerBench 9/15。完整逐题预测、目标与文件变化在 [baseline_predictions.jsonl](results/baseline_predictions.jsonl)，逐题分数、各类 F1、来源分组、错误 ID 与费用在 [baseline_report.json](results/baseline_report.json)。

Memory 的内容关键词检查即使在原文追加 Baseline 下也可通过，说明“覆盖词面”不能代替语义更新质量。这里的 20% 只表示动作与目标匹配，不宣称事实正确率或长期记忆收益；38 个写入题的内容诊断与完整 diff 单列，待盲审。

## 代表性失败案例

| ID | Baseline → Gold | 可观察原因 |
|---|---|---|
| `custom_rt_025` | continue → new | 句子提到了旧项目，但明确提出独立的新项目；词面重合掩盖任务身份 |
| `custom_rt_035` | select → clarify | 多份历史清单都可指代，Baseline 仍强选一个 |
| `custom_ml_005` | create inbox → update 原项目 | 用户已经进入验收，应替换项目状态；追加不能修正旧记录 |
| `custom_ml_023` | create inbox → no_op | 假设完成并不表示现实完成，原文拷贝会把临时假设塞入长期记忆 |
| `custom_pd_011` | notify → silent | 只看截止时间，没有检查事项已经完成 |
| `custom_pd_024` | notify → defer/silent | 没有核对用户当前专注状态 |

这些是实际 Baseline 错误，不能把它们当作完整 HeyBuddy 的真实失败，也不能推断 HeyBuddy 一定能修复。

## 生产接口 Smoke 与验证

先完成原 105 条结构检查及原有全套单元测试，再运行每 Track 三题 Smoke；之后扩展至全部 150 个快照。扩展验证通过，150/150 完成，无执行错误。固定 Provider 仅返回保守 clarify/空 Extraction/silent，不读取 Gold；最终记录 113 次本地 Provider 调用，**真实 API 请求 0、费用 USD 0、发送消息 0**。实际模型 Token 为未知，不伪造零 Token 或能力准确率。[契约报告](results/contract_report.json) 与 [逐题记录](results/contract_predictions.jsonl) 保留实际门控分支和调用数。

新增测试另外使用独立固定响应执行真实 Memory 提取、选文件、整合、临时写入和 applied 回执；验证嵌套标签泄漏拒绝、无工具调用、安静窗口、临时文件清理、非法 JSON、预算耗尽、缺失 usage 和推理不重复计费。不存在将 Gold 作为 FakeProvider 响应的“满分测试”。

交付检查：数据校验通过；最终全套回归 **1476 passed / 3 skipped**；新增契约测试单独运行 **14 passed**；Ruff check 与 format check 通过；`uv build` 及两个 CLI 的 `--help` 检查通过。首次沙箱执行受到临时目录/Windows 本地 socketpair 和构建联网权限限制；在保持测试外网禁用的执行环境中重跑，未修改测试保护。

## 真实模型待执行条件

本次没有执行付费模型，未提供可报告的完整 HeyBuddy 真实成绩或 Token/API Cost。需要环境中的 Key、明确模型/可选兼容端点、足够上下文支持、实际美元单价和请求/Token/费用预算。Key 不能写入命令参数、报告或仓库，不读取真实 `.env` 内容。运行命令见 [README](README.md#复现)。

建议先每 Track 三题 live，检查生产结构化输出和 usage，再显式扩大至各 50 题。Runner 使用独立临时数据、空工具请求、真实生产链路、SDK 无重试及共享总预算；真实 usage 缺失或预算耗尽即停止，部分结果仍保存，缺题留在分母。真实模型运行完后可用同一 scorer 对比本文的 Baseline；不能从当前离线结果估算完整 HeyBuddy 的语义收益。

仍待独立人工逐题审核，尤其是 TIAGE 相邻话题歧义、MAB 合成知识价值、Proactive 的“可通知但也可沉默”边界及 Memory 自由文本质量。Proactive 每题只有一个给定候选，尚未检验多机会竞争。当前数据适合作为有来源的轻量工程 Benchmark 草案，尚不足以支撑普遍能力结论。
