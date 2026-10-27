# Phase 2 Prompt Audit（Phase 1 后基线）

基线来自 Phase 1 提交 7ac3b6e1203cf58a1871ed8e16714e96bce912c8，原文固定在 evals/prompts/phase1-baseline.json。Ground Truth 与评分哈希在首次真实调用前写入 evals/manifest.json；之后不修改。模型均 deepseek-flash，同一参数、权限与合成数据，每开发场景重复两次；Holdout 尚不用于调整。

Baseline 开发集完成 24 次运行。观察不是仅凭 Prompt 长短推断；以下问题来自实际 trace，候选分组独立比较，最后组合在 Holdout 验证。程序评分的词面局限单独披露，不以改答案或评分器制造提升。

| 组/对象 | 当前行为与原因 | 候选修改及假设 | 可能退化 |
| --- | --- | --- | --- |
| Retrieval / Main | “任意命名 topic 先查记忆”使纯 Redis 假设讨论也查 Deferred/Recall/当前时间；长答导致一次源超页限制。Main 不知道 Host 已有自动提案，偏好确认后还要求用户手动 /memory edit | 明确一般问答/假设直接答；按 Core/Deferred 当前知识与 Recall 历史证据分流；默认简洁；只承认已证实写入，说明 Host 可做后续检查 | 过度直接回答可能漏查真实项目；因此保留项目索引与历史线索必查行为 |
| Retrieval / 根路径与缺失知识 | 多次 directory='.'、'/'、换行导致 invalid_path；缺失个人项目代号时重复失败查询，终轮出现未执行的工具标记文本 | 明确 root directory='' 与逐级路径；没有相关 lead 时承认无记录；不打印工具调用标记、不要重复同一失败调用 | 提前停止可能错过较深文件；评测仍要求实际读到项目主题 |
| Tool Descriptions | 参数和低信任警告重复，缺少“何时不用”的分界；部分成功说明较长但重要 | 保留 latest hash、唯一 old_text、部分写入含义与手工索引保护，缩短通用重复内容，注明普通问题不调用 Recall/索引审计 | 过度简短可能漏安全边界；Host 权限/路径/写入代码完全不变 |
| Worthiness | 重复已有项目知识的两次运行，一次仍进入完整 Reflection，另一次只做阴性筛查 | 只判断新增 durable evidence；现有 Core/已读取 Memory 足够且无更正则 false；不把助手科普当用户事实 | 若只有目录概要而没正文，可能误判重复；明确不确定既有内容时交 Reflection 比较 |
| Reflection Extraction | 稳定偏好两次存为“先讲设计动机...替代方案与权衡”，固定词面评分不命中 why/为什么等；主要含义正确，但额外扩张偏好值得审阅 | 保留用户原意，避免扩写未确认要求；合并来源、短引文和精炼理由 | 太精炼会丢技术背景；证据/来源和 Host 校验保持不变 |
| Selection / Consolidation | 基线项目纠正和去重均通过；多层通用指令和三阶段重复规则较长 | 相同主题优先已有文件、最新纠正原位替换、每文件一次修改；收紧为清晰决策顺序，减少重复 | 可能选择太少文件或忽略冲突；仍用相同 Schema/unique-span/hash 拒绝不安全结果 |
| Compaction | 基线长上下文两次均保留 B、失败和 release-check，没有已证实错误；旧 Prompt 对失败状态/最新更正不够明确 | 单独候选强调最新明确纠正优先、工具调用不是成功、未验证任务仍开放 | 增加输入 tokens 未必有收益；没有明确证据则保留原版，不为“都改过”而部署 |

开发集隔离分组：Retrieval 覆盖 direct_python、deferred_project、recall_discussion、memory_missing、hypothesis、tool_failure、cross_session；Worthiness 覆盖 duplicate；Synthesis 覆盖 stable_preference、project_decision、correction、long_context。每组只改变相应 Prompt，其他仍为 Baseline。稳定偏好词面分数不能单独证明语义改善，最终报告要人工对照证据和修改片段。

对照仍受单模型、小样本、输出随机性与严格 marker 的限制，不用模型自评取代权限检查；当前 rubric 不是完整事实质量证明。

## 最终选择（完成 Holdout 后）

保留 Main/Tool Descriptions/Worthiness；撤回 Reflection/Compaction 候选。选择在最终组合 Holdout 之前写入 evals/retention.json；Holdout 得分 7/8 → 7/8，未用于再次调整。一个虚构场景减少多余工具，但一个确认偏好在 Consolidation 误选 create 被拒绝；整体改善尚无证据。偏好词面得分提升并未消除额外扩写，拒绝将其视为语义提升。数值、阶段费用、全部限制见 [A/B 报告](prompt-ab-results.md)。
