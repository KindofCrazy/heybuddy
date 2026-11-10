# 自动化行为评测

新增独立的 [连续场景评测](sequence-evaluation.md)，覆盖真实 Router 候选缺失、持久 MemoryFS 多步更新和原话到 Scout/Attention 生命周期。数据与评分边界见 [Sequence v1](../../evals/heybuddy_bench/sequence_v1/README.md)；不替换原有 Snapshot Benchmark，也不将固定离线响应计为能力分数。

P3 增加默认无需 Key/网络的 [Character Evaluation](character-evaluation.md)，固定 13 场景与 20 轮漂移，对比 E0/E1/E2。复用本模块 Budget，合成历史/检索相同；机械契约与七维人工语义评分分开，只有 --live 才创建真实客户端。

## P1 独立 smoke

`evaluation/p1.py` 提供 `uv run python -m personal_agent.evaluation.p1 --live --max-requests 6 --max-tokens 196608`。没有 --live 时在加载配置前拒绝；--help 不读密钥。evaluate() 先建全新受隐私检查的合成目录，只生成固定四条带时间的聊天、普通 Markdown 和 SQLite；顺序调用 Episode/Thread、用合成 Working Summary 检查点模拟旧历史退出近期窗口，再运行真实 Agent/检索/Recall。无 Memory Apply、无自动 Memory 检查，不访问真实数据库。

SmokeProvider 在请求前复用 Budget.reserve()，记录 episode/thread/main/compaction 阶段与独立 usage，reasoning 仅是 output 的子集。summary.json 不含正文，private-review.json、原始数据库/Memory 只存忽略目录供人工复核。固定预期检查短时间话题分段、跨本地午夜、跨天 Thread、闲聊无 Thread、检索、Journal/Memory 不变；不根据模型得分改预期。corpus_hash/prompt_hashes 用于复跑比较。

该入口当前仅通过 FakeProvider 离线验证（tests/test_p1_smoke.py），没有执行真实模型；contract_pass 不能证明语义质量，报告始终带 semantic_review_required。完整复核清单与限制见 [P1 交付](../archive/p1-delivery.md)。原 16 Case、Holdout、scorer、baseline/retained 快照不改。

## 职责与架构组成

`evaluation/cases.py` 保存 16 个预先定义的合成场景、初始历史/记忆、用户输入和期望行为，覆盖 12 类任务，12 个开发 Case、4 个 Holdout。`scoring.py::score()` 固定程序 Rubric；权限、文件不变、历史完整、工具/路径、Action/来源等可机械检查，正文 marker 只作为有限语义代理。`prompts.py` 保存/安装评测专用快照；不改宿主权限或工具实现。`runner.py` 用真实 Agent、SQLiteStore、MemoryFS、Recall、ReflectionService 和 MemoryAutomation 执行。`__main__.py` 是显式付费入口，与正常 CLI 和 pytest 分开。`reporting.py` 只读取已收集的合成结果，重新校验评分并导出无正文的汇总。

## 机制流程

1. 先固定场景、真实答案依据、评分规则和预算，区分机制验证与模型效果比较。
2. 在隔离数据中执行基线与候选，分别记录输出、用量、失败和限制。
3. 按固定标准比较开发集与保留集，不能为提高候选分数改动评分规则。
4. 默认离线；真实付费评测必须显式授权，小样本结果不作为普遍收益。

## 代码实际运行路径

1. `python -m personal_agent.evaluation --list-cases` 仅列出清单；`--snapshot <新路径>` 调用 `prompts.capture()` 在隔离临时 SQLite 上注册工具，保存实际 Prompt/description，拒绝覆盖已有基线。这两个入口不加载真实密钥、不调用 API。
2. 只有 `--live` 才 `load_settings()`，允许显式覆盖 model/endpoint/context-window；快照来自 `--prompt-file` 或当前源码。价格和调用/token/cost 上限由参数提供，非法预算拒绝。SDK max_retries=0。
3. `live()` 在 `.heybuddy/evaluations/run-<variant>-<随机ID>` 创建全新目录并检查 Git 隐私。每个 Case/重复独立 `evaluate_case()`，最多两个并发 Case，同一 Case 的 Agent 工具仍按原顺序执行。
4. `evaluate_case()` 初始化真实临时数据库和 Agent Memory Root，seed 合成历史和主题文件。长上下文场景 seed 16 个完整 Run；跨会话场景新建 Conversation，并用旧会话标记检查上下文泄漏。其他场景默认无 Memory 写权限，Automation 始终 suggest，不执行 Apply。
5. `TraceProvider.generate()` 在调用前由 `Budget.reserve()` 保守预留输入/输出，超出 request/token/cost 任一上限就停止。实际返回后记录阶段、usage、耗时、正常文本和工具调用；不导出原始 reasoning。模型响应仍经正常 Journal 保存完整 Provider JSON，仅位于忽略的合成数据库中。
6. 正常答案后复用 MemoryAutomation；生成计划仅观察，不批准。检索走真实 Registry/Recall/MemoryFS；失败工具是固定 demo_status。比较执行前后 Markdown 和历史，收集计划/来源校验结果，`score()` 计算固定检查。`result.json` 与逐 Case 更新的 `report.json` 保存私有原始轨迹。
7. `reporting.load_run()` 校验完整集合、无重复、相同模型/参数/语料/Prompt 哈希，重算固定评分；`campaign()` 仅将相同 Case/重复配对。普通请求、Compaction、Memory Automation 分开统计，reasoning 是 output 子集，不重复计费。结果导出 `evals/results/summary.json`，没有回复正文、引文或密钥。

## 运行与复现

先配置本地 `.env`，不要在命令行粘贴 Key。以下真实调用会收费；snapshot/list/reporting 不收费。若更换模型或参数，重跑对应基线，不能直接与这次结果混合。

```powershell
uv run python -m personal_agent.evaluation --list-cases
uv run python -m personal_agent.evaluation --snapshot .heybuddy/current-prompts.json
uv run python -m personal_agent.evaluation --live --variant baseline-dev --split dev --prompt-file evals/prompts/phase1-baseline.json --repetitions 2 --max-requests 128 --max-tokens 2000000 --input-price 0.3 --output-price 1.2 --max-cost 1.5
uv run python -m personal_agent.evaluation --live --variant retained-holdout --split holdout --prompt-file evals/prompts/retained-candidate.json --repetitions 2 --max-requests 40 --max-tokens 800000 --input-price 0.3 --output-price 1.2 --max-cost 0.8
uv run python -m personal_agent.evaluation.reporting --output evals/results/summary.json
```

上例显式限制 baseline-dev 为 128 requests/2M reserved tokens/$1.5；总实验额度见 manifest 与 [A/B 结果](../archive/prompt-ab-results.md)。命令中价格是本次峰值 cache-miss 估算，复现前按真实服务商校对。`--model`、`--endpoint`、`--context-window` 可显式设置；`--cases` 逗号列表支持分组，`--concurrency 1` 可串行。重复 variant 时 reporting 会拒绝混合多次实验；通过 `--output` 为新 campaign 选独立的忽略目录，reporting 的 `--root` 选相同目录。

## 异常与边界

预算失败不自动扩大/重试；失败普通 Run、失败 Automation 和未完成采集分开记录。未知 usage 标为不完整，费用只代表已知部分，不是账单。隐私检查在目录创建后、数据写入前执行；自定义输出必须在仓库外或被完整忽略。不要提交原始 result.json、SQLite 或 Memory。汇总仅有计数、时长、安全错误/检查名与哈希。

manifest 保存 Phase 1 checkpoint、冻结语料与收集器哈希、基线哈希、预算和价格。收集结束后仅纠正了 automation_requests 的报告分类：Compaction 不再误算为自动记忆；原始 stage、运行逻辑与评分不变。manifest 同时记录前后收集器哈希，汇总从原始 stage 重算。

小样本和单模型不能证明普适提升；词面命中不能证明完整语义质量。本轮没有 LLM Judge，不用模型自评证明权限或事实正确；偏好扩写与 Holdout invalid_create 经过人工复核，并保留失败。开发分组不是最终组合在全部开发集的复测，不能合成一个“最终总准确率”。Holdout 不用于再次调 Prompt。

## 历史问题驱动的 Prompt 复核

`evaluation/prompt_audit.py` 是独立的空工具探针入口。`stages()` 读取主对话、Episode、Thread、Learning、兼容角色状态、Scout、Attention、Composer、检索、Curator、目标选择、局部整合、手动 Reflection、Worthiness 和 Thread Repair 的当前提示词及实际输出模型。`prompt_audit_cases.py` 与 `prompt_audit_workflow.py` 保存合成开发、正向控制和最终独立案例；它们取自历史暴露的问题类型，不复制私人消息或身份。Rubric 是人工审阅要求，不用关键词命中或模型自评声称语义准确率。

执行顺序为 `main()` → `run()` → 检查输出目录隐私 → 保存不可变提示词快照 → 无 `--live` 直接返回。显式 live 才通过正常配置入口加载模型与 Key；不输出配置值。读取同一目录的 ledger，预算按已尝试请求累计，默认 30，最大允许显式配置 100。每次网络请求前追加并 flush 计数；失败也占用额度。SDK `max_retries=0`，每个案例只调用一次空工具 `OpenAIProvider.generate()`，最多输出 4096 tokens；不执行 Agent、Registry、Memory Apply、Scheduler 或 Sender，不读取生产数据库。当前入口按单进程顺序使用，同一 campaign 不并发启动。

完成后按真实 Pydantic 模型校验 JSON，保存结构校验结果与成功响应。API 异常仅保存安全类型、Provider code 和可用的数值 usage；不保存未完成正文或异常体。原始合成结果、提示词快照与 ledger 只留在忽略目录，历史版本不覆盖；重复案例必须用新 variant 显式重跑。结构校验不等于来源校验或完整生产路径通过，引用和语义仍需人工核对。reasoning 是 output 子集，不重复计费；未知价格不估算费用，不完整 usage 不当作零费用。

离线保存快照示例：`uv run python -m personal_agent.evaluation.prompt_audit --output .heybuddy/new-audit --variant snapshot`。获得真实调用授权后，可加 `--live --split dev --max-requests 100`；其他 split 为 holdout、positive、workflow、workflow-positive、fresh，`--stages` 可限定环节，`--prompts` 可加载原版快照作同输入对照。新环节不在旧快照中时沿用当前默认并单列实际 prompt_hash，不能宣称旧快照覆盖该环节。

本轮收敛在主对话、Episode、Learning、兼容 Markdown 角色状态和 Composer；Thread 试验未见稳定收益而撤回。Luna persona.json 当前不运行 CharacterStateService，状态探针只验证兼容环节。Holdout 中暴露的草稿冲突后来用于修订，因而不再视为最终独立测试；另设 fresh 固定案例在停止调参后比较原版与最终版。保留的局限包括摘要偶有多余助手复述、同一事项重复 highlight、个别知识回答数值不一致；不能以合法 JSON 宣称全语义正确。本轮未改固定评分器、Compaction 或任何生产数据。无正文汇总见 `evals/results/history-prompt-audit.json`。

离线契约测试在 `tests/test_prompt_audit.py`，覆盖 snapshot 无需配置或客户端、100 次累计上限在调用前拒绝、失败请求计数且不自动重试；`tests/test_evaluation.py` 继续冻结历史快照和后台记忆规则，仅新增主对话表达快照。API 探针基于采集时工作区提示词，不替代最终提交的离线接线测试，也不保证跨模型收益。

本轮累计 86 次请求，82 次通过输出结构检查，另有 2 次 ProviderError 和 2 次 JSON ValidationError；失败均计入用户授权的 100 次上限。已知 input/output/reasoning 分别为 103171/55458/47171 tokens，usage 不完整且没有单价，不报告费用。最终 fresh 原版/候选五组逐项配对使用相同 case_hash；兼容状态的助手故事反馈循环和草稿冲突合并在这组案例中得到改善，条件偏好仍保留，但这不是总体语义准确率或跨模型收益结论。

## 全流程 API 调用清单与复核

`evaluation/flow_cases.py::ORDER` 固定十九类模型调用的合成案例顺序，`flow_cases()` 提供开发、正向控制、定向复核和留出输入。字段按实际调用封装：检索使用 question/candidates，Curator 使用 memory/evidence_by_path，主对话将时间数据与用户原话分开注入。`prompt_audit.stages()` 绑定实际生产提示词与 Pydantic 输出模型；`--split flow-dev|flow-positive|flow-focus|flow-holdout` 进入这些案例。JSON 校验后，`validate_contract()` 对来源/分段复用实际 Episode validate 和 Reflection verify_candidates，并检查字节预算、ID 归属、候选覆盖和局部编辑精确匹配。schema_valid 与 contract_valid 分开记录，二者都不能代表自然度或全语义正确。

| 模型调用 | 生产入口 | 触发与检查重点 |
|---|---|---|
| 表达完整性判断 | channels/qq/completeness.py::judge | hybrid 模式，ready/wait/uncertain，不回答问题 |
| 检索预选 | retrieval.py::ContextRetriever._preflight | 有候选时选择相关且合法的来源 |
| Compaction | core/compaction.py::compact_step | 预算触发，保留纠正和执行状态，满足字节上限 |
| Main Agent | core/agent.py::Agent._run | 回答与工具结果续轮，自然、准确，不冒充成功 |
| Episode | organization/episodes.py::EpisodeOrganizer.organize | 完整 Run 分段、摘要、引用与重要内容 |
| Thread 组织 | organization/threads.py::ThreadOrganizer.organize | 跨段当前状态，避免累计流水账 |
| Thread 修复 | organization/threads.py::ThreadOrganizer.repair | 仅从当前成员和本轮 checkpoint 重建 |
| Learning 抽取 | learning/service.py::MemoryLearningService.propose | 可复用事实、更正和有条件偏好 |
| Learning 目标选择 | 同上 LEARN_SELECT 请求 | 全部候选恰好分配一次，不能代替拒绝环节 |
| Learning 局部整合 | 同上 LEARN_CONSOLIDATE 请求 | 每目标一次，精确修改并保留无关内容 |
| Curator | learning/curator.py::MemoryCurator | 小组记忆无损去重，事实变更需来源 |
| 兼容角色状态 | character/state.py::CharacterStateService | 防自我强化；当前 Luna persona.json 不走此分支 |
| Scout | proactive/scout.py::OpportunityScout.extract | 用户计划、取消、完成与稳定 topic_key |
| Attention | proactive/attention.py::AttentionEngine.evaluate | 交流价值和时机，无价值时 silent |
| Composer | proactive/composer.py::Composer.compose | 来源一致、自然、克制、外层 JSON |
| 手动 Reflection 抽取 | reflection/service.py::ReflectionService.reflect | 长期价值与真实用户证据 |
| 手动 Reflection 选文件 | 同上 SELECT 请求 | 已有相关主题及冲突来源，不造路径 |
| 手动 Reflection 整合 | 同上 CONSOLIDATE 请求 | 全候选覆盖、局部计划，不能冒充批准或写入 |
| 兼容 Worthiness | automation/service.py::MemoryAutomation | P2 关闭时判断新持久信息，避免滥触发 |

主对话可能多次调用模型：每次工具结果返回后是下一轮请求；图片理解属于同一主模型的多模态输入，不是新增事实提取服务。普通 Markdown 读写、SQLite Recall、时钟、有限中文提醒解析、提醒队列和到期处理本身不调用模型。QQ 取 token、图片下载/上传和消息回复、NapCat 本机 WS/OneBot 投递是另一类网络 API，不属于模型推理计数；本轮不对真实账号或真实消息发送做验证。

`evaluation/flow_runtime.py` 通过显式 `--live` 调用真实 Agent、Provider 和仅含本地算术/时间的 Registry，验证工具调用/结果续轮及无 shell 工具的权限边界，再用内存生成的合成 JPEG 验证图片分支。所有实际模型轮次都写入与 prompt_audit 相同的 ledger，最大一百次，失败或取消同样计数，不并发运行同一 campaign。`--judge-only` 通过生产 judge 函数验证实际短超时；`--judge-budget 2` 仅为实验对照，不修改生产配置。纯模型探针不开工具，不创建审批、生产 Memory 或 Sender；真实 Agent 探针也不提供写入/删除工具。

模型每例默认输出上限 4096，完整性判断为 64 且 reasoning_effort=none；本轮发现 Episode 在 4096 下未完成后，Episode/Thread/Repair 探针改为 8192，留出两版本使用相同上限，不能把预算差异当作提示词收益。API 返回只保存完成正文；异常仅安全类型/code 与可用 usage。CLI 正常退出表示完成采集，不表示全部语义通过。不可变提示词快照、原始合成回复、工具结果和 ledger 只保存在忽略目录；无正文汇总为 `evals/results/full-flow-audit.json`。

测试顺序为两组十九环节基线、六次定向对照、四次补充复核、十九组留出原版/最终配对，再做真实 Agent/图片与判断器时限测试。留出结果不再用于调参。工作区有并行开发时，按实际 prompt_hash 保留来源，其他环节或输出 schema 的并行变化不能归因本次修改，也不合成一个总体准确率。结构/契约检查与人工语义观察分别汇报；旧采集缺少覆盖检查时，由同一宿主契约复核输出并单独记录，不覆盖原始行。

首批 94 次保留共享抽取规则、Learning 目标完整覆盖和 Composer 自然度/来源约束；当时 Episode 去重和 Thread 精简试验未稳定奏效而撤回。主要余项包括 Episode 复述/重复、Thread 未知细节、主模型额外建议、图片精细形状误判，以及判断器冷请求在一秒预算下可能回退 uncertain。相同两例在两秒实验预算中得到 ready/wait，不足以证明延迟 SLA，不自动修改默认。对应无网络契约见 `tests/test_flow_audit.py`；与运行时采集器一样，它们验证工程边界，不伪造模型语义成绩。

## 新增额度后的定向迭代

用户另授权一百次请求后，以新 ledger 追加 73 次，两个额度实际合计 167 次。`flow_revision_cases.py::revision_cases()` 冻结开发、正向控制、held-a/b 和后续 stop 案例；每组核心六环节为主回复、Episode、Thread、Repair、Compaction、Composer。开发和控制共 36 次，held-a/b 原版/候选同输入、同 schema 各十二次，稀疏 Thread 对照两次，末版主回复 stop 对照四次，真实 Agent/图片七次。六十六个纯探针通过结构和所实现的来源/覆盖契约；这不是语义准确率。两个正文不入库的汇总分别为 `evals/results/full-flow-audit.json` 和 `evals/results/flow-revision-audit.json`。

新版 Episode 优先保留用户计划，将措辞更正并入同一个 highlight，保留有意义的明确反馈；摘要也剔除未接受的清单、催汇报、无关角色生活。Thread/Repair 不因信息缺省创建下一步，用户明说的不确定性仍保留。工作摘要只保存原文提出的下一步，不能由“暂不发布”推导发布任务；不处理另行解决的 Compaction 连续失败。主回复增加按请求结束与能力说明的终点：无法实际操作时不自动提供命令或服务邀约。Composer 进一步去掉“不催你”“按你的节奏”“心里有数”等管理进度口吻，不扩写来源没说的地点、形式或结果。

实际观察是局部改善：交稿计划与更正不再重复成两个重要条目，角色任务未进入重要内容；修复状态不再添加等待发布时间，工作摘要不再补上线任务；最后一次真实 Agent 的无 shell 回复未提供命令或邀约，真实加法仍能两轮完成。新留出里 Episode 摘要仍偶有无关助手插话，稀疏 Thread 仍出现“准备情况未说明”，主回复也会多列替代方案，图片精细形状误判继续存在。不能宣称所有环节均已稳定达标；未改判断器默认时限。

完整离线回归发现通用交谈尾文会随角色全文进入 Composer，引发输入预算失败；`Composer.compose()` 只移除末尾精确匹配的 `CONVERSATION_PROMPT`，保留角色表达且不修改调用者原数据。交谈 Markdown 的 CRLF/LF 在加载时统一为 LF，避免不同 checkout 下提示词哈希和快照不一致。`tests/test_flow_audit.py` 覆盖这两个实际接线边界。历史快照不改，末版组合另存 `flow-revised-context.json`。

复现时使用新忽略目录，先以 `--split revision-dev|revision-control` 开发，冻结后才跑 `revision-held-a|revision-held-b`；`--stages` 可以限定环节。最后主回复通过 `revision-stop|revision-stop-positive` 单独复核。`flow_runtime --tools-only` 只重测真实工具续轮与能力说明，不重复图片；`--judge-only` 仍独立。相同 campaign 顺序运行，入口最大一百次、调用前计数，不自动续预算或重试。

## 范围与最小状态的后续迭代

再次授权的一百次请求使用独立 `.heybuddy/prompt-iteration-20261031` ledger，实际 94 次：91 个纯探针和三次真实 Agent 轮次。`prompt_iteration_cases.py::iteration_cases()` 提供四环节的开发、正向控制、早期留出、英语迁移和最终 fresh 案例，`prompt_audit --split iteration-*` 复用原有请求前计数、无自动重试、来源与覆盖校验。`iteration-held-a/b` 暴露问题后用于修订，已转为开发诊断；后设 `iteration-fresh-a/b` 在冻结后做原版/候选/显式重复对照，不能把前面的留出称为最终独立结果。

本次保留通用交谈的范围结束规则与 Thread/Repair 的最小事实规则。比较 A/B 时不额外讲 C，一种方法不扩成多条路线；明确请求多个方案时仍逐项回答。状态不再先列目标/进展/待办栏目，只保留来源支持且仍相关的内容；未接受建议不因加了“尚未确认”说明而保留，本页细化同一安排时替换旧泛称。最终新留出与重复中，只给表达式/两句话的要求得到遵守；旧助手建议从选纸安排中移除，“是否带样稿未定”仍保留。真实 add_numbers 两轮完成，能力不足说明没有附送命令或邀约。

Episode 尝试过合并重复规则、选择性摘要和明确删留示例；少数案例变干净，但新留出 B 两次仍重复拆条目，并出现把未发生的“误谈比赛”归给助手。因此该试验撤回，文件恢复任务前版本，原有未提交实现保留。五次 Episode 请求耗尽 8192 输出预算未完成，原版和候选均出现；没有加生产预算或把失败当通过。英语迁移中，Thread 在中文标题混合输入下仍使用中文，原版也存在，不宣称已解决。

无正文结果在 `evals/results/scope-focused-audit.json`，记录全部请求元数据、失败和保留/撤回决定，不计算整体语义准确率。最终组合快照另存 `evals/prompts/scope-focused-context.json`，历史快照和历史评分不改。`tests/test_flow_audit.py` 验证案例封装、未知 split 拒绝与共享额度，`tests/test_evaluation.py` 仅允许当前组合的主回复规则变化；默认 pytest 禁网保护保持。所有真实测试使用合成数据，没有生产历史重算、Memory Apply 或真实发送。

## Episode 质量、语言与真实上下文复核

后续授权取消了总请求上限；单个入口仍按 ledger 限制最多一百次，通过独立忽略目录分组，失败和开发试验全部计数，不自动重试。此轮实测见 `evals/results/episode-quality-audit.json`，原始合成正文、数据库、快照与 ledger 仍只在忽略目录。历史结果和固定评分器不改，不把结构通过率写成语义准确率。

此轮实际 381 次请求，已知 input/output/reasoning 为 1061918/160702/127705 tokens，推理属于 output 子集，没有价格配置，不报告费用。最终 Episode 六十次通过结构及来源/分段契约；保留主对话组合的三组八轮测试遵守技术题范围，但一轮代码后的补充推论仍有错误。最后追加的代码语义规则未稳定改善组合表现，且再次出现多余建议和“不催你”，因此撤回，保留前一冻结组合；不据此宣称事实准确率或自然度达到百分之百。完整离线验证在排除用户原有未提交改动的暂存导出上运行，1322 通过、3 跳过，Ruff 与构建通过；没有改生产压缩预算来迎合测试。

`evaluation/episode_quality_cases.py::quality_cases()` 提供 quality-dev/quality-holdout，共二十个固定案例，覆盖角色插话、虚构归责、事项合并、真实工具结果、取消、有条件偏好、跨午夜日期、必分边界、长主题和不可信指令。`language_cases()` 提供三组正文/标题语言冲突及技术名称案例。`prompt_audit --case-id` 可选择一例，`--reasoning-effort none|minimal|low` 明确控制探针参数。输出保存 case_id、参数、时长、schema/contract 检查和哈希；工具投影通过 `flow_cases.turns()` 还原真实内部消息，去除投影标记而保留工具调用/结果，必分边界继续调用生产 Episode validate 校验。

先分别比较同一旧提示词的 none/low，再在同一 low 下比较旧版与按独立事项提取的新版。none 虽快但出现来源问题，因此未作为生产选择。最终 Episode 组合以二十例各重复三次复核；早期留出暴露问题后已转为开发诊断，不宣称六十次都是独立留出。Thread/Repair 的三组语言案例用默认推理参数作旧版对照和新版重复，避免把参数收益混入提示词收益。日期锚点取对应用户 Run 的 local_started_at，不取后续助手重复或 Episode 结束时间。

`evaluation/quality_runtime.py::run()` 先拒绝缺少 --live，再加载正常 Host 配置；pipeline 模式复用 `evaluation.p1.evaluate()`，隔离 SQLite/Memory 并经过实际 Episode、Thread 和 Agent 检索调用。conversation 模式用真实 Luna、ContextBuilder、Agent 和 provider_data 回放顺序执行八轮；image 模式把合成图片交给同一条角色主对话链，不增加视觉专用模型。Meter 在每次请求前持久计数，保存输出预算、effort、系统/输入哈希、时长和 replay 数量；SDK 禁止自动重试。多轮输出会改变后续输入，所以这类对照是行为回归，不是固定输入 A/B。

实际 Host 的 Organizer 输出上限是 393216，早期 8192 耗尽发生在探针，不应误报为生产配置。此轮没有提高生产输出上限或超时；只增加可选 Episode 专用 effort，并在已验证的本机配置设 low，库级默认仍不发送该参数。图片试验中四倍缩放没有纠正误判，未进入生产；最终规则使无尺寸依据时使用外观类别，有明确等边直角条件时仍准确命名。这是确定性校准，不证明精细视觉能力增强。当前 QQ 聚合配置为 time，历史一秒 hybrid 判断回退属于可选路径，本轮不修改其默认或宣称解决延迟 SLA。

显式复现示例：`uv run python -m personal_agent.evaluation.prompt_audit --live --output .heybuddy/new-quality --variant q1 --split quality-holdout --case-id local_midnight --reasoning-effort low --max-requests 100`。真实调用链用 `uv run python -m personal_agent.evaluation.quality_runtime --live --mode pipeline --variant p1 --output .heybuddy/new-runtime --max-requests 100`；另可选 --mode conversation|image、--case-set fresh。每次使用新 variant，不覆盖失败记录；--image-scale 仅为评测图片生成参数，不改变生产图片处理。

`tests/test_episode_quality.py` 离线验证默认与显式 effort、仅 Episode 转发、真实来源和必分边界、不完整 usage 安全诊断、视觉规则单 System/不入历史及无 live 时不加载配置；`tests/test_qq_images.py` 核对真实图片角色装配路径。当前主对话组合另存 quality-reviewed-context.json，历史快照保留。本轮未重算生产历史、执行 Memory Apply、连接真实发送或修改 Compaction 的连续失败机制。现有进程需重启才能加载新代码、提示词和配置。

## 阅读与验证

`tests/test_evaluation.py` 离线验证真实 Runtime/Automation 的 Fake 路径、检索工具、预算调用前拒绝、不可被好答案覆盖的安全失败、快照一致、Compaction 计量分离和不完整采集拒绝。默认 pytest 保持网络禁用，真实评测仅通过单独显式命令运行。

## 未来改进

把机制正确性、模型语义质量、成本与回归风险分开报告。固定 ground truth 与保留集，防止为了候选分数调整 scorer。
