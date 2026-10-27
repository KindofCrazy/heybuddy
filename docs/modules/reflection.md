# Reflection 与长期记忆整合

## 职责与架构组成

V0.7 的 Reflection 是用户手动触发的学习流程：从原始对话提取可复用理解，与现有 Markdown 比较，生成计划，用户明确批准后才写入。它不维持当前模型窗口，不读取 Conversation Summary 作为事实，也不启动新的 Agent Loop。

| 文件 / 符号 | 实际职责 |
| --- | --- |
| `reflection/models.py` | ReflectionPolicy、Evidence、Candidate、Decision、PlanItem、ReflectionPlan 的有界模型及安全错误 |
| `reflection/service.py::ReflectionService` | 完整 Run 分页、三阶段 Provider 调用、证据验证、计划生成、可信批准和逐项写入/核对 |
| `reflection/memory.py` | 有界目录元数据、选定记忆读取、精确片段计划及索引核对 |
| `reflection/writer.py` | 手动 Reflection 与 Host P2 共用的受限单文件发布及只读核对；不授予权限 |
| `reflection/cli.py::command`、`render` | 可信命令分派与私人 Before/After Diff、理由和来源展示 |
| `storage/reflection.py::ReflectionRepository` | 绑定 Agent 的 SQLite 查询、计划修订冲突检测、独立审阅进度 |
| `main.py::_run_session`、`chat` | 复用同一 Provider/MemoryFS 装配服务，在普通 Run 之外执行命令 |

SQLite proposals 是审阅记录，不是第二套长期 Memory 权威源。成功应用后仍由普通 Markdown 决定后续 Core/Deferred 内容；Reflection 不改变 ToolRegistry、不注册可被模型调用的审批工具。

## 参考与架构取舍

参考本地 Letta Code checkout `6c8b08fedfb6f3fb437163ede8320a387151693f` 的 [reflection-v2.md](https://github.com/letta-ai/letta-code/blob/6c8b08fedfb6f3fb437163ede8320a387151693f/src/agent/subagents/builtin/reflection-v2.md)、maintaining-core-memory、maintaining-deferred-memory：优先从纠正和稳定模式学习，先读相关记忆，一个主题保留一个主要位置，整合理解而不追加聊天日志，Core 精简并通过分层索引定位详细知识。

上游的 Reflection 是后台子 Agent，可自主维护 Memory/Skills 并提交 Git；HeyBuddy 采用显式 CLI、结构化提案和人类审批，不复制后台调度、Bash/Edit 工具、Git Worktree、提交合并、自动 Skill 学习或多 Agent。来源保存在 SQLite 审阅记录及原始历史；不采用依赖 Git 保存修改理由的上游方案。

## 代码实际运行路径：提取到计划

1. `main.run_cli()` 原子迁移到 schema 4，恢复正常 Run，初始化 MemoryFS；`_run_session()` 把与 Agent 相同的 Provider、MemoryFS、ContextBudget 及 ReflectionPolicy 传给 ReflectionService，Repository 绑定 agent_id。
2. `chat()` 识别 `/reflect`，调用 `reflection.cli.command()` → await `ReflectionService.reflect(current_conversation_id)`。这些命令不创建普通聊天 Run，不给下一次输入附加 Memory 工具权限。
3. 若当前 Conversation 已有未关闭计划，直接返回它，不再次请求模型。否则读取 reflection_progress 的独立 through_sequence；不复用 context_summaries 的压缩边界。
4. `ReflectionRepository.window()` 先确认 Conversation 属于当前 Agent，再复用 `storage/context.py::page(completed_only=True)`，以 context_runs 索引获取最多 page_runs 个完整 Run；先检查 payload 字节、消息数，再读取并验证配对。failed/interrupted/cancelled/running 均不作为 Reflection 源，V0.6 的 completed/failed 重放规则不变。
5. 按完整 Run 前缀选择能放进提取输入预算的历史。给模型的证据包含 Conversation、Run、实际 sequence；文本原文有有限凭证脱敏，工具参数/结果投影沿用 V0.6 的有截断标记限制，Provider 原始 reasoning/密文不提供给 Reflection。整个 Journal 不改动。
6. `_ask(Extraction, EXTRACT, history)` 使用空工具列表、独立输出限额与超时，要求 JSON Schema 对应的结构化候选。每条 Candidate 包含类别、主题、内容、长期价值理由、确认程度、Evidence 列表。允许空候选。
7. `_verify()` 验证 Conversation/Run/sequence 属于本次提供的范围，引文是实际展示证据中的精确子串。confirmed 至少包含用户文本证据；只有助手陈述不能作为用户确认。程序验证来源的存在与归属，不证明引文在语义上足以支持结论，仍需人工审阅。
8. 有候选时，`catalogue()` 复用受限 Memory 路径/全树数量上限，读取 Core 和 Deferred 文件的 name/description、目录概要。第二个 `_ask(Selection, SELECT, ...)` 只选至多 6 个已存在路径；不存在或重复的选择被拒绝。没有把所有 Deferred 正文都送入 Prompt。
9. `read_comparison()` 完整读取三个 Core 及选定文件，第三个 `_ask(Consolidation, CONSOLIDATE, ...)` 比较已有知识，逐候选提出 create/update/no_op/reject/review。每阶段输入都含实际 Schema 和封装开销并重新检查预算；目录或选定正文太大时明确失败，不静默省略比较材料。
10. `plan_item()` 用宿主读取的原文计算 before_hash/after_hash。update 只能修改已读取目标，old_text 非空且精确唯一；create 只能创建受限主题文件，含合法 Frontmatter、不覆盖现有文件。校验 Core 编译总容量及单文件大小。同一计划不接受同一文件的多次写入；模型应在提取时合并相关候选，或用 relations 说明依赖并标为 no_op/review。
11. 不确定候选强制 review，不能通过 apply 升级为确定事实。创建主题与已有 name/文件 stem 完全碰撞时转为 review；近似重复、时间纠正及事实价值仍是模型判断，用户审核最终 Diff。no_op/reject/review 不携带隐藏写入片段。
12. `ReflectionRepository.save(expected_revision=None)` 在短事务内重新读取本批有界源，验证当前审阅进度及 Run/hash 前缀未变，然后保存待审阅计划。生成计划不修改 MemoryFS。空候选计划直接关闭，并原子推进已处理范围，避免反复提取无价值历史。

## 审批、写入与恢复

`/reflect review [plan_id]` 显示候选内容、类别/确认程度、长期价值、整合理由、关联候选说明、原始 Conversation/Run/sequence/引文、目标路径、before_hash、局部 Diff、逐项状态和结果。创建还明确提示会生成缺失的目录索引并补齐到根的链接；自动索引使用现有中性目录概要，保留用户原说明。

只有可信 CLI 或直接宿主 API `apply(conversation_id, plan_id, item_id)` 可以批准。CLI 必须带具体 plan_id 和 item_id/all，防止一个无目标的 apply 意外批准下一份计划。review 不授予权限，普通对话、模型输出、Tool Result、Summary 和 Markdown 中的“用户已批准”均不能调用这条入口。

应用路径按项顺序执行：

1. `_plan()` 校验计划归属、是否仍未关闭。选择 pending/approved 项；已应用、已拒绝、已跳过不会再次执行，冲突/部分完成/不明确状态也不会自动重试。
2. 先把 approved 和 approved_at 保存到 SQLite（后续拒绝/跳过也保留批准时间）；实际写前再保存 applying。若这两步失败，MemoryFS 尚未执行。no_op/reject 的用户确认只记账，不写文件；review 保持待判断，用户可 skip/reject 后在新历史中给出明确纠正。
3. create 调用现有 `index.create()`；Core update 调用 `MemoryFS.edit()`；Deferred update 调用 `index.edit()`。保留 expected_hash、唯一匹配、路径/Agent 隔离、Git 隐私检查、Core 容量、短锁和单文件原子发布。没有新的 Memory Store。
4. 成功记录 applied 与 saved_hash；索引部分失败记录 partial、已保存 hash 及父索引原因；其他 Memory 错误记录 conflict，并保留未完成计划。多项更新不做整体回滚，前面成功的结果保留。
5. 最后一个条目成为 applied/rejected/skipped 时，计划关闭与 reflection_progress 推进在同一事务完成。未决计划阻止同一 Conversation 再次提取；一旦全部审阅完成，下一次从新边界继续。已关闭计划仍可用原 ID review。

SQLite 与 Markdown 没有共同事务。文件成功后写入结果的数据库事务失败时，持久状态仍为 applying；真实进程退出也可能留下这个状态。再次 apply 不重做。`/reflect reconcile <plan_id> <item_id>` 只读取文件核对 after_hash 和逐级发现链，不写文件：匹配则记账为 applied，索引仍不完整则 partial，内容不能确认则 uncertain。对于元数据编辑引起的部分成功，还验证具体父条目与当前 name/description/目录概要一致，避免“链接存在但说明过期”被当作成功。

部分创建可先 `/memory check`、显式 `/memory repair` 补链，然后 reconcile。人工索引说明不会被 Reflection 覆盖；若内容仍需人工决定，应编辑核对或 skip 该条目。skip/reject 已部分写入的项只关闭剩余计划，result_code 会说明此前可能已经写入，不撤销文件。conflict 需要用户 review 后 skip/reject；在新的明确历史中重新提案，不复用旧审批和 hash。

编辑器修改目标后，apply 拒绝旧 hash；删除目标同样冲突。外部编辑器不参与 MemoryFS 锁，仍保留最后检查到 replace 的竞争窗口，没有自动合并或重试。

## SQLite Schema 4

| 对象 | 字段与约束 |
| --- | --- |
| reflection_progress | conversation_id 主键/外键、through_sequence；独立于 Compaction |
| reflection_plans | id、conversation_id 外键、closed、revision、payload JSON、checksum；单份 payload ≤262144 UTF-8 字节 |
| one_reflection_plan | 每 Conversation 最多一份 closed=0 计划 |
| reflection_plan_conversation | 有界活动计划查询索引 |

`storage/sqlite.py::MIGRATIONS[4]` 只追加对象，原始 messages/runs、Recall 与 context_summaries 不改写。计划 JSON 包含已校验来源的 Run/hash、候选、证据和修改片段；没有另存整段聊天。读取核对 checksum/外层身份/修订/状态，更新用 revision 做乐观冲突检测。计划、证据及审阅记录都是私人明文，沿用数据库与附属文件的 Git 忽略和备份策略；checksum 用于错误检测，不是对本机用户的认证。

## 预算、错误及限制

- 默认每次手动处理至多 8 个完整 completed Run、最多 8 个候选；一次 `/reflect` 处理一批，不循环清空全部历史。非空候选最多三次模型请求，空候选一次；没有自动重试、后台扫描或续跑。
- 每阶段默认独立输入 16384、API 总输出 16384 tokens、可见 JSON 32768 字节、超时 60 秒。输入进一步受模型窗口减输出与安全余量限制。沿用保守 UTF-8 JSON 输入估算；API 总输出含推理，可见 JSON 单独受 response_max_bytes 字节上限约束，两个单位不混用；不是精确 tokenizer。返回 Tool Calls、空/非法 JSON、越界字段、明显凭证、超预算 usage 或无效证据时明确拒绝，原进度不推进。
- 历史单 Run 无法装入预算时明确停止，不拆配对，不截断用户正文。catalogue 受原全树 500 项上限约束，选定文件各受 MemoryFS 大小上限约束，整个阶段超预算就拒绝；较大记忆库尚无多轮检索规划，应调预算或整理主题。
- 模型价值判断、近似去重、纠正与引文的语义关系不能靠 Schema 完全保证；离线测试验证结构、来源和审批契约，不代表真实模型必然正确学习。人类审阅是必要边界。
- 原始对话和 Memory 都是不可信参考，提示词不能保证模型绝不受注入影响。程序隔离工具与批准入口；有限凭证模式不是通用秘密检测，用户也应检查私人内容和新记忆。日志/错误不输出正文，review 才显式展示私人候选及引文。
- 默认当前 Conversation，跨会话通过 /resume 后分别反思；同一 Agent 的长期 Markdown 共享，因此后一会话会与前一会话形成的知识比较。未实现跨会话自动聚类、向量检索或全库 Reflection。
- 同一进程 CLI 串行命令，短同步 SQLite/文件步骤不交给取消后继续跑的线程；只在 Provider await 期间取消。取消生成时不保存不完整计划，已持久计划可下次恢复。没有多 Agent 并发工作流、计划自动改写、历史版本恢复、Background Tasks 或 Scheduler。

## 阅读与验证

最小离线路径见 `tests/test_reflection.py::test_preference_evidence_approval_and_future_context`：临时 SQLite → seed completed Run → FakeProvider 三阶段 JSON → reflect → 确认 Markdown 未改 → review → apply → 新会话 Core 最新值。无需真实 Key 或在线 API。

真实子进程验证见 `test_cli_plan_restart_apply_and_crash_after_file_publication`：第一次 CLI 生成计划退出，第二次明确批准、文件发布后 os._exit，第三次恢复状态、禁止重复写入并 reconcile。其他测试覆盖纠正、重复/no-op、不可靠证据、选择/路径、分页、空候选、审批前取消、迁移回滚、SQLite/MemoryFS 失败、部分索引、人工编辑、Agent/Conversation 隔离及原始日志不变。

## V0.7 实际验证记录

最终完整离线回归：390 passed、1 skipped（Windows 环境不能创建 Symlink，原 Junction 测试仍通过）。Ruff 检查、格式检查、uv build、personal-agent --help 与 python -m personal_agent.main --help 通过。没有真实模型请求、API Key 或费用；tests/conftest.py 网络禁用保护保持原样。

构建的 0.7.0 wheel 在三个独立 CLI 进程中验证：第一进程以 Fake Provider 三次无工具请求生成待审阅计划，两个原始消息保留，Markdown 未改；第二进程恢复计划、review 并明确 apply，无模型请求，批准时间与独立进度 2 保存；第三进程普通请求读取已批准的新 Core，一次 Fake 正常回答后原始消息数由 2 增至 4。另有自动化真实 os._exit 测试覆盖文件发布与结果写库之间退出、重启禁止重复写入并只读 reconcile。

## V0.7.1 预算与安全诊断修正

旧实现使用 output_tokens=4096 同时限制 API 总生成 tokens 和 JSON 字节，真实推理模型既容易用尽总额度，也可能生成正常 JSON 后被错误的字节比较拒绝。现在 ReflectionPolicy.output_tokens 默认 16384，response_max_bytes 默认 32768；_ask() 在 API 调用与 usage 校验使用前者，在正文限长使用后者，再做结构/来源校验。

TokenUsage 增加可选 reasoning_tokens；OpenAIProvider 在拒绝非 completed 响应时只传出 ProviderDiagnostics：固定枚举 status、incomplete_reason（max_output_tokens/content_filter/unknown）及非负用量。_ask() 通过 ReflectionError 附加 extraction/selection/consolidation 阶段，chat() 仅打印这些安全字段；失败仍不保存计划、不推进边界、不执行工具或重试，未完成正文及推理内容不进入错误信息。

EXTRACT/CONSOLIDATE 明确区分有长期价值的纠正与一次性假设/虚构示例的免责声明；没有对应错误事实时，不把后者扩写成永久用户规则。提示词改善仍需真实提案审阅，不能用 Fake Provider 测试宣称语义质量保证。

测试增加正常中文 JSON 超过旧 4096 字节但在独立上限内可接受、JSON 仍有硬上限、总 usage 超限继续拒绝、Provider 不完整原因与推理计数脱敏传递，以及 CLI 在 consolidation 失败后保留原记忆/进度。真实模型的诊断和复测结果见[真实验证记录](../reflection-live-validation.md)。


## V0.7.2 Phase 1 集成

V0.7.2 自动提案复用同一 ReflectionService；reflect(through=...) 限定已判断范围，人工调用不传时行为不变。apply 的可信 approval_source 记录 human 或 host_exact_grant，不向模型暴露。Worthiness 成功阴性筛查可以无提案地推进已审阅范围；完整来源与状态区分见[Automation](automation.md)。

## V0.7.2 Phase 2：未部署的候选与语义限制

BASE/EXTRACT/SELECT/CONSOLIDATE 经过固定开发集对照后保留原版。候选偏好分数提升主要来自词面命中，未消除未经确认的偏好扩写，阶段费用反增；不是已证实的语义改善。最终 Holdout 另有一次 Consolidation 对已存在 human.md 返回 create，被 invalid_create 拒绝，普通答案/历史保留，无 Markdown 写入。Worthiness 正确也不代表整条提案必定正确，人工 review 和 Host 校验继续承担不同边界。

所有候选/原版快照、失败说明及自动/普通费用分列见 [A/B 报告](../prompt-ab-results.md)。本轮没有为了消除 Holdout 失败而再调 Prompt 或修改评分标准。
