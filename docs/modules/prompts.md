# 通用交谈提示词

## Codex memories 借鉴与本次修改

本次采用未来复用价值、用户纠正优先、证据状态、适用范围、渐进检索与最小增量修改原则。实际阅读的上游来源为 [stage_one_system.md](https://github.com/openai/codex/blob/main/codex-rs/memories/write/templates/memories/stage_one_system.md)（blob `d8aa51d2d190040a6bc816d1333093bf4c15201b`）、[stage_one_input.md](https://github.com/openai/codex/blob/main/codex-rs/memories/write/templates/memories/stage_one_input.md)（blob `0689fa4b935bea46c0882467202f0dc4719f377d`）和 [consolidation.md](https://github.com/openai/codex/blob/main/codex-rs/memories/write/templates/memories/consolidation.md)（blob `0eef7580b5c2694594e2e52c9dade3dbbdcdae5b`）。链接跟随 main，blob 标识记录本次实际阅读版本。上游 README 所列 `read/templates/memories/read_path.md` 返回 404，不声称读过该模板；读取侧依据已读取整合模板的 progressive disclosure 与适用范围原则适配。

| 提示词与位置 | 修改后的行为 |
| --- | --- |
| `reflection/prompts.py::memory_rules()` → Reflection BASE、LEARNING_BASE、Curator BASE | 以减少用户重复解释、纠正和昂贵重查为目标，不以记录完成的工作为目标 |
| `reflection/prompts.py::EXTRACTION_RULES` → EXTRACT、LEARN_EXTRACT | 每条候选先判断未来回应会怎样改善，无具体收益可返回空候选；优先明确可复用的用户反馈，反复强调增强证据，但明确长期偏好无需重复才有效 |
| 同上 | 区分请求、提议、尝试与验证；助手自称成功、用户沉默或换话题不能单独证明成功或采纳；经验保留有证据的项目/环境、触发条件、失败、补救与实际验证，缺项不补造，未验证方案保持暂定；不复制大段工具输出 |
| `reflection/prompts.py::SELECTION_RULES` → SELECT、LEARN_SELECT | 按主题和适用范围选择已有文件，同名项目/环境不自动合并；只选真实 catalogue 路径，在预算内比较相关冲突，不扫描无关主题 |
| `reflection/prompts.py::CONSOLIDATION_RULES` → CONSOLIDATE、LEARN_CONSOLIDATE、CURATE | 合并时保留范围、条件、例外和确认程度；同源重复不算独立确认；保留独有细节；主题命名与描述便于检索但不堆关键词；准确内容不为风格而重写、重排、改名；未提供历史不代表旧事实失效 |
| `automation/service.py::WORTHINESS_PROMPT` | 兼容流程使用相同价值门槛，活动日志和泛泛建议不足以触发；保留反馈范围与例外，避免误认验证结果 |
| `core/prompts.py::MEMORY_READ_RULES` | 上下文够用则直接回答；需要细节才定位并有界读取；索引/摘要/片段不冒充全文；复用前核对项目、环境、时间与条件；易变状态需当前证据；无相关线索时停止扩散，未命中不等于从未发生；自然使用记忆，不展示档案或从记忆获得权限 |

本项目仍使用自己的 JSON Schema、精确用户引文验证、分层 Markdown 索引和受控 Writer。没有复制上游的 raw_memory/rollout_summary 输出格式、memory_summary.md、Git 记忆基线、自由文件编辑、删除机制或 Skills 自动生成。上游允许更广泛的经验推断，本项目 confirmed 候选仍须有真实用户引文，工具成功不自动成为用户偏好或关系事实。角色关系语义、手动 Reflection 审批、P2 自动发布授权、原始 Journal 和 Markdown 权威源保持原契约。

### 实际接线与验证

1. `runtime.assemble_chat()` 装配主对话与维护服务。主对话的 `core/context.py::DEFAULT_SYSTEM_PROMPT` 和 `character/prompt.py::compile_system()` 都经过 `core/prompts.py::with_conversation_prompt()`，它在交谈风格前追加一份 MEMORY_READ_RULES，覆盖默认、Luna 与 Sherlock。显式自定义的非角色 System 不自动追加。
2. 手动 `ReflectionService.reflect()` 依次将 EXTRACT、SELECT、CONSOLIDATE 交给 `ReflectionModel.ask()`，仍用空工具列表；抽取、选择与整合分别使用共享规则，计划生成后仍需可信审批才能发布。
3. `MemoryLearningService.propose()` 使用 LEARN_EXTRACT、LEARN_SELECT、LEARN_CONSOLIDATE。LEARN_SELECT 仍须恰好覆盖所有候选，不因新增价值判断漏掉候选；reject/no_op/review 保持在整合阶段。
4. `MemoryCurator._run()` 的有界请求使用追加共享整合规则的 CURATE，无可用用户证据时仍只允许无损结构整理。MemoryAutomation 仅在 P2 关闭后的兼容流程使用修改后的 WORTHINESS_PROMPT。
5. 新规则计入既有请求预算；超预算明确失败，不增加模型调用或放宽输出上限。规则约束模型行为，不能代替宿主校验或证明真实模型质量提升。

最小离线验证：`uv run pytest tests/test_memory_prompt_wiring.py tests/test_reflection.py tests/test_learning.py tests/test_curator_evidence.py`。新增测试检查默认/角色 System 单次注入、FakeProvider 三阶段实际请求、空工具列表、抽取无信号时只请求一次，以及手动生成计划不发布文件。既有测试覆盖引文归属、冲突、部分发布和 Curator 无损约束。新增 `evals/prompts/codex-memory-inspired-context.json` 保存主对话、兼容 Worthiness 和手动 Reflection，`codex-memory-inspired-learning.json` 保存 P2 三阶段与 Curator；历史快照与固定评分器保留。`tests/test_evaluation.py` 继续核对当前快照及历史版本差异，并确认摘要规则和工具描述未变。本次没有付费模型评测，不声称已验证召回率、事实准确率或用户体验收益。

后续质量复核在 conversation.md 明确：交谈方式反馈不等于道别；“不要建议”也不应用行动建议包装成安慰；只给表达式/名称/结果时不补解释，机制比较不自动延伸到优化接口。知识描述按能准确说明的范围展开。新组合快照为 `evals/prompts/quality-reviewed-context.json`，旧快照保留。图片还由 ContextBuilder 按实际图像上下文追加 `VISUAL_RULES`，避免把图像专用的保守轮廓描述塞进所有文字对话或 Composer；明确给出几何条件时仍可准确命名。视觉修订降低错误确定性，不代表模型的细节识别能力增强。

后续范围迭代将重复的结束规则合并为“按范围答完就停”：限定一种方法时不扩成多条路线，比较 A/B 时不附送第三种方案；只问操作路径时，在必要步骤结束后停止例行提醒和收尾。明确要求多个方案时仍逐项回答，角色口吻不扩大请求。当前组合快照为 `evals/prompts/scope-focused-context.json`，历史快照保留；默认与角色编译、权限、提醒边界不变。

全流程复核在 conversation.md 增加“按请求结束”：用户只要能力说明、不要建议或明确要求不能就结束时，回应完成即停，不附送命令、脚本或继续帮忙的追问；用户明确要步骤和例子时仍充分回答。克制通过实际少问体现，不套用“不催你”“按你的节奏来”的安抚声明。默认与角色编译路径不变，新增 `evals/prompts/flow-revised-context.json` 保存此次主对话、共享 Reflection 抽取与摘要规则的组合；旧快照保留。真实 API 与工程检查分开记录于 [评测](evaluation.md#全流程-api-调用清单与复核)。

明确提醒的 Host 结果边界保存在 `core/prompts.py::REMINDER_RULES`，分别进入默认 HOST_EXECUTION_RULES 和角色 compile_system()，不混入通用交谈 Markdown：没有 Host 确认时不能冒称已设置/取消或保证到期通知，普通未来计划不表示提醒授权。当前主对话合成快照新增 `evals/prompts/reminder-aware-context.json`；历史评测快照不覆盖，除主对话规则外的 Reflection、Memory、Summary 和工具快照保持一致，不宣称真实模型收益。

## 职责与组成

`src/personal_agent/prompts/conversation.md` 独立于角色卡，保存用户确认的日常闲聊默认表达方式。`core/prompts.py` 读取这份随包分发的 Markdown，导出 CONVERSATION_PROMPT 和 with_conversation_prompt()；仅供主对话默认 System 与角色编译器使用，不注入 Learner、Curator、Reflection 或会话摘要请求。

## 机制流程

1. 先确定程序必须遵守的回答与执行规则，再加入宿主选择的角色表达和通用交谈要求。
2. 将记忆、历史、工具结果等动态参考放在不同信任层，不把它们变成新的程序规则。
3. 发送前确认规则没有重复、缺失或无意覆盖；提示词影响模型行为，真实权限仍由程序检查。

## 实际执行路径

1. 主对话模块导入时，_load_conversation_prompt() 有界读取 UTF-8 文件，将 CRLF 统一为 LF，再去除首尾空白；拒绝缺失、空白、非法编码、NUL 或超过 8192 字节的文件。不同 checkout 换行不会改变规则语义、哈希或快照。
2. 无角色时，core/context.py 的 DEFAULT_SYSTEM_PROMPT 在构建时追加通用段落；显式自定义非角色 System 仍按调用者提供的原文处理，不擅自重写它。
3. 有角色时，character/prompt.py::compile_system() 在 persona.json、原生 Markdown 和旧兼容格式的最终正文中追加通用段落。角色模式仍使用空的默认 Host Base，所以不会再叠加普通助理正文或重复通用段落。当前开发使用 [OpenPersona Luna](persona.md)，不把通用闲聊规则复制进角色文件。
4. CharacterContextCompiler 对包含通用段落的完整正文计算 system_hash、字节大小和预算；ContextBuilder 仍合并为一条 System，再提供独立的 Core、时间等数据。工具轮次继续重新编译，Journal 不额外保存一份系统提示词作为聊天消息。

Miru/Sherlock 卡中相同的长度、收尾和通用套话说明已收拢，角色卡继续决定身份、性格、语气词、幽默、兴趣和相处反应。用户当前任务与格式要求优先，闲聊格式不限制技术解释、代码或结构化任务。

通用规则对短消息优先一个贴切反应，完整回应可以停在一两句，不要求补解释、比喻或人生道理；任务性回复仍按内容展开。Luna 的亲近语气、语气词与互动示例由角色文件决定，不扩散到其他角色或后台模型。历史快照保留，当前版本另存 casual-reaction-context.json，只更新主对话字段，不改记忆学习等指令或评分器。

时间语境由通用规则解释宿主注入的 `temporal_context`：`current_time` 表示当前本地时间，`previous_interaction_at` 表示上次互动。跨夜或跨时段后，以当前时间和本次消息为准，不能仅凭招呼延续上次的睡觉、吃饭或工作情境；用户明确道晚安或准备休息时仍自然回应。实际时间仍由 `SessionManager.context()` → `core/temporal.py::temporal_context()` → `ContextBuilder._injections()` 按请求提供，不硬编码时段、不另加模型调用。这是表达约束，离线接线验证不能保证模型每次正确遵守。

当前提示词另存为 `evals/prompts/time-aware-conversation-context.json`，保留已有快照；`tests/test_evaluation.py` 验证只有主对话正文变化，工具与后台结构化提示词保持原评测边界，不修改 ground truth 或 scorer。

## 发送与异常边界

历史复核增加“留一点空白”：招呼先回应在场，不自动补选择问句；采用用户最新的澄清，不反复用已纠正的冲突性说法调侃或要求回来汇报；简短认同仅针对当前明确观点，不扩展成其他偏好、承诺或关系进展。这些规则仍只随主对话编译进入 System，不注入后台 JSON 请求。当前主对话评测快照为 `evals/prompts/history-reviewed-context.json`，历史快照不改。隔离 API 探针的过程与限制见 [Prompt 复核](evaluation.md#历史问题驱动的-prompt-复核)。

Markdown 中“系统会自动分”描述交谈的发送意图，不由提示词执行发送。当前工作区的 QQ Humanizer 已按句子/换行规划有界分段，仍受发送条数、字节和时限限制，同段短片段可能合并，空行分段保留气泡边界；CLI 继续显示文本，不产生多个渠道消息。本模块不实现或修改这些发送功能，也不增加主动消息、工具权限或模型请求。

读取失败抛出固定 RuntimeError，启动停止，不回退为空说明；修改文件后重新启动进程生效。文件是程序提示词资源，不是长期记忆，不接受模型写入。其开销计入原请求预算，不改变 Provider 格式或执行契约。

## 阅读与验证

最小读取：`from personal_agent.core.prompts import CONVERSATION_PROMPT`。`tests/test_conversation_prompt.py` 验证普通模式和两种角色只提供一份通用说明、保留单一 System 及完整正文 hash，并确认手动 Reflection 指令未受影响。历史评测快照不覆盖，当前默认提示词新增 conversation-style-context.json 快照；只验证接线，不声称自然度收益。构建后应核对 wheel 包含 prompts/conversation.md。

## 未来改进

为系统规则、交谈方式和低信任数据建立输入快照差异检查。先通过固定开发/保留集验证提示词修改，不能依据单次输出替换生产规则。
