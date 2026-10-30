# 通用交谈提示词

## 职责与组成

`src/personal_agent/prompts/conversation.md` 独立于角色卡，保存用户确认的日常闲聊默认表达方式。`core/prompts.py` 读取这份随包分发的 Markdown，导出 CONVERSATION_PROMPT 和 with_conversation_prompt()；仅供主对话默认 System 与角色编译器使用，不注入 Learner、Curator、Reflection 或会话摘要请求。

## 实际执行路径

1. 主对话模块导入时，_load_conversation_prompt() 有界读取 UTF-8 文件，拒绝缺失、空白、非法编码、NUL 或超过 8192 字节的文件。
2. 无角色时，core/context.py 的 DEFAULT_SYSTEM_PROMPT 在构建时追加通用段落；显式自定义非角色 System 仍按调用者提供的原文处理，不擅自重写它。
3. 有角色时，character/prompt.py::compile_system() 在 persona.json、原生 Markdown 和旧兼容格式的最终正文中追加通用段落。角色模式仍使用空的默认 Host Base，所以不会再叠加普通助理正文或重复通用段落。当前开发使用 [OpenPersona Luna](persona.md)，不把通用闲聊规则复制进角色文件。
4. CharacterContextCompiler 对包含通用段落的完整正文计算 system_hash、字节大小和预算；ContextBuilder 仍合并为一条 System，再提供独立的 Core、时间等数据。工具轮次继续重新编译，Journal 不额外保存一份系统提示词作为聊天消息。

Miru/Sherlock 卡中相同的长度、收尾和通用套话说明已收拢，角色卡继续决定身份、性格、语气词、幽默、兴趣和相处反应。用户当前任务与格式要求优先，闲聊格式不限制技术解释、代码或结构化任务。

通用规则对短消息优先一个贴切反应，完整回应可以停在一两句，不要求补解释、比喻或人生道理；任务性回复仍按内容展开。Luna 的亲近语气、语气词与互动示例由角色文件决定，不扩散到其他角色或后台模型。历史快照保留，当前版本另存 casual-reaction-context.json，只更新主对话字段，不改记忆学习等指令或评分器。

时间语境由通用规则解释宿主注入的 `temporal_context`：`current_time` 表示当前本地时间，`previous_interaction_at` 表示上次互动。跨夜或跨时段后，以当前时间和本次消息为准，不能仅凭招呼延续上次的睡觉、吃饭或工作情境；用户明确道晚安或准备休息时仍自然回应。实际时间仍由 `SessionManager.context()` → `core/temporal.py::temporal_context()` → `ContextBuilder._injections()` 按请求提供，不硬编码时段、不另加模型调用。这是表达约束，离线接线验证不能保证模型每次正确遵守。

当前提示词另存为 `evals/prompts/time-aware-conversation-context.json`，保留已有快照；`tests/test_evaluation.py` 验证只有主对话正文变化，工具与后台结构化提示词保持原评测边界，不修改 ground truth 或 scorer。

## 发送与异常边界

Markdown 中“系统会自动分”描述交谈的发送意图，不由提示词执行发送。当前工作区的 QQ Humanizer 已按句子/换行规划有界分段，仍受发送条数、字节和时限限制，同段短片段可能合并，空行分段保留气泡边界；CLI 继续显示文本，不产生多个渠道消息。本模块不实现或修改这些发送功能，也不增加主动消息、工具权限或模型请求。

读取失败抛出固定 RuntimeError，启动停止，不回退为空说明；修改文件后重新启动进程生效。文件是程序提示词资源，不是长期记忆，不接受模型写入。其开销计入原请求预算，不改变 Provider 格式或执行契约。

## 阅读与验证

最小读取：`from personal_agent.core.prompts import CONVERSATION_PROMPT`。`tests/test_conversation_prompt.py` 验证普通模式和两种角色只提供一份通用说明、保留单一 System 及完整正文 hash，并确认手动 Reflection 指令未受影响。历史评测快照不覆盖，当前默认提示词新增 conversation-style-context.json 快照；只验证接线，不声称自然度收益。构建后应核对 wheel 包含 prompts/conversation.md。
