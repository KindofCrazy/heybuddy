# Schema、Ground Truth 与评分协议 v0.2

本页下文保留 v0.2 协议。当前 v0.4 仍使用相同输入接口，新增状态评分扩展如下；运行命令和完整局限见 [v0.4 README](v0_4/README.md) 及 [本轮报告](ITERATION_REPORT.md)。

| v0.4 Gold / manifest 扩展 | 含义 |
|---|---|
| `storage_alternatives` | 预先声明动作与已有目标的合理替代存储，不能从候选输出反推 |
| `optional_extra_paths` | 允许有证据的额外文件修改，其余修改仍扣范围分 |
| `preserve_terms`、`fact_assertions` | 必须保留的指定内容及跨文件条件；后者按 `paths`、`all_any`、`none_exact` 在旧状态与真实 diff 重建结果上检查 |
| `review_not_before` | 在原生未来时间校验之外检查延后复查的最早允许时刻 |
| `total`、评分器 SHA-256 | 校验实际总数，分别冻结 state / fact / temporal scorer |

主分由 `cycle.summarize()` 选择版本化 scorer：Memory 合法目标、新值别名、指定旧原文移除和声明的保护条件必须同时通过；Proactive defer 检查实际持久化时间与目标。原动作分仍保留。跨文件条件不是完整语义检查，词面否定、额外合法学习和同义词仍可能误判，已发现实例进入复核队列，不能据最终保留输出静默改分。

主集仍每 Track 50 题；另外 30 题为相关的开发/保留变体，4 道事后探针单独报告。v0.4 的 MAB 四题已被个人知识题替换；LongMemEval 旧状态为规范主题笔记；TIAGE 接受更宽的闲聊解释；未来约定与 DND 允许满足真实时间条件的内部 defer。旧 source ID、native_gold 与适配记录均保留。

一个 case 是独立决策点。`inputs.jsonl` 每行包含 `case_id`、`track`、`source`、`category`、`scenario_group`、`review_status`、`input`；只有 `input` 进入生产适配器。`gold.jsonl` 按相同 ID 保存 `gold`、可选 `provenance` 和审核说明。公开 `source_id`、原生标签、`has_answer`、TriggerBench `variant/function/ground_truth` 和审核信息均不进入模型。

`manifest.json` 固定两份数据与公开摘录的 SHA-256。冻结 JSONL 显式写入 LF，Git 属性也固定 LF，防止 Windows/Unix checkout 改变哈希。校验使用显式异常，不使用可被 `python -O` 移除的 assert。拒绝重复/缺失 ID、非法动作、未来证据、无时区时间、跨目标引用、非唯一 update 片段、路径穿越、缺失来源与哈希变化。

## Thread Routing

输入：`now`、`active_thread_id`、`thread_candidates[{id,title,current_state,last_active_at}]`、`history[{episode_id,timestamp,thread_id,messages}]`、`recent_context[{timestamp,thread_id,text}]`、`current_message`。

输出对齐真实 Router：`continue|select|new|chat|clarify`。Router 实际返回绑定 Thread，适配器根据持久化绑定、先前 Thread 与 `metrics.reason/ambiguous` 归一化；不向 Router 添加评测专用 Prompt。`chat` 允许创建“日常交流”Thread，`clarify` 仍记录底层绑定，`selection_unavailable` 作为执行错误。

Gold：`action`、`thread_id`、可选 `acceptable_actions`。`continue/select` 校验目标；`new/chat/clarify` 不比较任意新建 ID。一个 TIAGE 狩猎话题允许 continue/new 两种合理解释。新建与恢复的任务身份根据实际改编输入判断，不直接把 topic-shift 标签翻译成 new。

原 40 题包含恢复、相似 Thread、子任务、新项目、社交与模糊指代；修订了十个恢复句子的表达、伪“刚才”的时间、无性能上下文的模糊题；候选顺序固定打散，自建 Thread ID 改为无类别含义的 UUID。新增相同领域新目标、活跃 Thread 下仍有两个可指代版本两题。

## Memory Learning

输入：`now`、`memory_files{relative_path:content}`、`memory_catalogue`、`episode{id,thread_id,events[{id,timestamp,speaker,text}]}`。快照映射为有真实角色与时间的临时 completed Journal Runs；默认确认回复只是夹具，不能作为用户证据。缺 Frontmatter 的旧主题由适配器加中性元数据，不改变正文；MemoryFS 自己维护索引。

生产链路原生输出是 Extraction → Targets → FileDecision → PlanItem → 写入/回执。Runner 不强迫模型输出规范化 `facts`，收集真实写入后的文件变化及整合决定；不写真实 Memory。失败写入不伪装成 no_op。多文件变化完整保留，单目标题不会只选“最好的一项”计分。

Gold 保留 `action:create|update|no_op|reject|review`、`target_path`、`old_text/new_text`、`evidence_ids`、种子中的辅助 `facts`，另有 `content_checks`。已有目标路径精确比较；新建主题名无法从输入唯一确定，因此只比较合法 `topics/` 区域，同时保留实际文件名。没有候选的生产 Extraction 不产生 reject 标签，临时/假设题允许 reject/no_op；未确认冲突允许 review/no_op。严格原始动作分与可接受动作分分别报告。

内容检查是“每组至少命中一个别名、所有组都命中”的确定性诊断，不是语义 Fact F1。报告保留完整 diff 供盲审；不能将正确动作、路径或一个关键词解释为长期记忆质量。索引完整性、exact-span、来源、隐私和回执由真实 Pipeline 验证。新增替代题覆盖助手话语不是用户事实、已存事实、截止日更正、项目限定偏好、关系反馈、引用攻击、实体隔离及未经确认的冲突。

LongMemEval 使用 `oracle` 中指定用户证据裁剪，预载较早原文并输入之后的更正；这是 evidence-only 更新测试，没有原 QA 查询，也没有全库检索难度。MemoryAgentBench 使用四个不同单跳关系，保留每个关系的全部修订顺序；合成知识显式标为用户整理的设定笔记，不当作真实人物事实。

## Proactive Decision

输入：`now`、`attention_context`、`recent_user_evidence[{id,timestamp,text}]`、`candidates[{id,title,topic_key,status,context,due_at,source_evidence_ids,last_notified_at,followup_state}]`。生产原生决定为 `silent|defer|notify|propose_task`，Attention 恰好选一个有效候选；Host gate 的 silent 可没有候选选择。

适配器将候选及其原始用户来源放入临时 Proposal Repository，再调用真实 `ProactiveRuntime.run()`：安静时段、终结状态、covered、既有通知后没有新证据等由 Host 过滤；其余走 Attention、版本复核、CAS 和 Shadow 审计。`last_notified_at` 映射到候选已评估状态；当前可见的新证据可重新评估。这是可观察快照夹具，不是真实投递回执。

`attention_context.availability` 本身没有生产消费者，不偷偷变成新 Prompt；忙碌自建题补充了真实可见的合成用户专注陈述。`due_at` 也不是原生 Proposal 字段，模型只能从带时间的用户证据理解它；Baseline 使用此快照字段。报告明确这个接口差异。常规未来计划不创建 Scheduler。

Gold：`action`、`proposal_ids`、`next_review_at`、`evidence_ids`、`as_of`、可选 `acceptable_actions`。推迟题允许合理 silent；只要 defer 就必须有未来有效复查时间，比较 Host 持久化的 `effective_next_review_at`，不强求原 Gold 的精确分钟。silent 不因 Host 过滤产生空 ID 而扣目标分。

TriggerBench 原始正负标签与正例参考意图只在 provenance 内保存。正例并不必然等于 HeyBuddy notify：两道午夜题按默认 23:00–08:00 安静窗口重标为 silent。负例先读真实状态变化或条件未满足证据；不向模型暴露 positive/negative 或意图 summary。上游 Scout 的提取/覆盖能力与提醒文案质量不在本 Track 的评分范围。

## 方法、分母与成本

每 Track 仅一个确定性 Baseline 与生产 HeyBuddy。Routing 基于词面重合与最近性；Memory 把用户原文追加到单一 inbox；Proactive 只按 24 小时内截止决定 notify，不作状态/打扰复核。Baseline 不用模型，因此成本为零，不宣称同 backbone 控制实验。HeyBuddy 真实运行使用一个配置的 Provider，按各生产阶段的原生上限计费。

输出包括严格 Action Accuracy、固定标签集 Macro-F1、Acceptable Action Accuracy、Action + Target 的 Decision Accuracy、目标准确率、有效动作率、来源分组、逐题错误。Proactive 另报 silent 上的误打扰率及 notify/propose_task 精度。内容检查单列。缺题、未知动作和执行错误均留在分母；缺题过多的报告不能作为完整评测。Wilson 95% 区间只作描述，模板题不独立，不用于强推广结论。

`smoke` 的固定保守输出与 Gold 无关，只检验接线；报告禁止生成能力分数。`live` 逐题记录调用、实际 usage、估算输入、延迟和按显式价格计算的成本；缺失 usage 为 null，停止后续真实请求。推理 Token 已在 output 中，单列但不额外收费。不记录 SDK 原始异常体、隐藏推理、凭据或 Provider 私有 payload。
