# Context Budget 与 Compaction

全流程 API 复核收紧 `SUMMARY_PROMPT` 的内容选择：下一步与未决问题只有来源明确提出时才保存，不因“尚未发布”推导“下一步上线”，不把助手催汇报或虚构工作写成用户任务。这次仅调整摘要语义，不处理另行排期的连续 Compaction 失败、重试或恢复行为；预算、校验与持久化路径不变。合成对照入口与记录见 [评测](evaluation.md#全流程-api-调用清单与复核)。

P1 的 SUMMARY_PROMPT 将摘要明确限定为仍影响当前对话的工作目标、约束、进展与待续信息，保留来源时间、未确认建议和最新明确更正。存储/checksum/CAS、保护近期完整 Run 和原日志均不变；既有 context_summaries 继续使用，不删除或重建。可选 Thread/Episode/raw 检索只使用触发水位内剩余预算，不为旧参考材料额外启动压缩，见 [Retrieval](retrieval.md)。

## 职责与架构组成

`core/budget.py::ContextBudget` 定义宿主明确指定的模型窗口、输入/输出预算和资源上限；`estimate_request()` 对内部消息、Provider JSON 和工具定义做保守估算。`core/compaction.py::compact_step()` 只负责一批完整历史 Run 的无工具摘要。`core/context.py::ContextBuilder.prepare()` 在每次正常 Provider 请求前决定是否压缩、保存进度、重建工作上下文。Core 不导入 SQLite 或 SDK。

`core/history.py` 定义 ConversationSummary、HistoryTurn、HistoryPage 和 ConversationHistory Protocol；SQLiteConversationHistory 绑定 Conversation ID，将读取/检查点提交转给 `storage/context.py`。InMemoryHistory 为原有短嵌入/离线测试提供同一接口，但不提供重启持久性。CLI 始终使用 SQLite 和显式 ContextBudget。

## Context 与 Memory 的 WHY

SQLite messages 保存完整、原始经历，Recall 从它查询历史证据；Core/Deferred Markdown 保存跨会话长期知识。Compaction Summary 仅属于一个 Conversation，是可被新摘要替换的工作上下文，不是新的长期事实库，不写 MemoryFS，也不更新 Recall。这样压缩不会把一次任务的临时状态自动变成所有会话都遵守的事实。

参考本地 Letta Code checkout `6c8b08fedfb6f3fb437163ede8320a387151693f` 的 [local compaction](https://github.com/letta-ai/letta-code/blob/6c8b08fedfb6f3fb437163ede8320a387151693f/src/backend/local/compaction.ts)：借鉴保留近期窗口、增量合并摘要和不可缩减时明确停止。HeyBuddy 使用更容易验证的完整 Run 边界，不复制上游切分策略、备用模型、重试或文本截断回退。

## Budget 与边界

输入上限 = context_window − reserved_output_tokens − safety_margin。触发水位 = 输入上限 × trigger_ratio。每次检查涵盖 Base、最新 Core、已有 Summary、历史窗口、当前用户/assistant/tool 消息，以及本轮真实提供的工具定义；provider_data 也完整计入。重复的内部/Provider 表达会被重复计量，偏向高估。

估算采用序列化 UTF-8 字节数、每条消息的固定余量及请求余量，不是精确 tokenizer，也不能证明任意模型的隐藏输入一定在真实窗口内。诊断中的 estimated_tokens 与 Provider 返回的 input_tokens/output_tokens 分开记录；未知模型不猜窗口。模型可能有额外服务端计量，安全余量需由使用者按端点校准。

正常请求发送 max_output_tokens，摘要请求有独立输出限额和超时。根据 [OpenAI Responses/Reasoning 文档](https://developers.openai.com/api/docs/guides/reasoning)，该上限也包括 reasoning，不能当成可见正文长度。adapter 设置 truncation=disabled，避免服务端静默丢弃前文；服务端拒绝仍按安全 ProviderError 处理，不自动重试。

`recent_runs` 是必须保留的最少近期完整 Run 数量，不是平时只加载这么多条。未达到压力且一页能容纳时，保留整个未压缩窗口；超过水位或有尚未加载的后续历史时才尝试压缩。分页上限本身也是资源触发条件，防止小消息数量无限增长而绕过有界读取。

压缩目标最多到倒数第 recent_runs+1 个可重放 Run 的末尾。当前 Run 从不成为摘要源；completed/failed 才可作为源，cancelled/interrupted/running 全部排除。摘要覆盖的是可重放序列中的前缀，允许跳过不可重放 Run 的 sequence 空隙，不允许跳过一个正常已结束 Run。一个历史 Run 内的调用/结果永不拆开作为模型重放窗口。

## 代码实际运行路径

1. `main.run_cli()` 迁移当前 schema 4（Context 表由 migration 3 引入），恢复遗留 running Run，构造带 Settings.context_budget() 的 SessionManager；切换会话会构造绑定新 Conversation 的 ContextBuilder。
2. `Agent.run()` → `ContextBuilder.begin()` 原子创建本 Run 和原始用户消息；此时不全量读历史。读取失败也能留下可诊断的本轮状态。
3. 每轮 `Agent._run()` 先根据权限/终轮策略生成工具定义，再 await `ContextBuilder.prepare(messages, definitions, provider)`。
4. 首轮 `_load()` 读取一行摘要及其边界，从该 sequence 之后按完整 Run 读取有界页。后续同 Run 请求复用页缓存，Core 仍每次重新编译。
5. `for_request()` 返回 Base → Core user JSON → Summary user JSON → 未被摘要覆盖的完整历史 → 当前 Run 消息。Summary 的 kind=conversation_summary、trust=historical_data_only，不插入原始 Journal 或 pending。
6. `estimate_request()` 计算当前请求。若不需要压缩且不超预算，直接返回；有压力时查询安全旧历史目标。
7. 存在可压缩前缀则调用 compact_step()。读取最多 page_runs 个完整 Run，逐个尝试加入摘要请求；超过摘要输入预算前停止，绝不发送超预算的整段原始会话。
8. 摘要请求只有固定 SUMMARY_PROMPT 和低信任 JSON 证据，包含旧 Summary 与这批新 Run 的 ID/sequence/消息。Provider 原始 reasoning/密文不传给摘要器；工具结果/参数使用有截断标记的有限证据投影，原始内容仍可 Recall/审计。常见密钥赋值、password/API key/token 字段与 Bearer 形式先作有限脱敏，提示词要求不复制秘密和注入指令。
9. `provider.generate(..., tools=[], max_output_tokens=summary_output_tokens)` 在独立 compaction_timeout 内 await。任何 tool_calls、空白、超长、明显凭证模式或超预算 usage 都拒绝。绝不执行摘要返回的调用。
10. `save_summary()` 在短事务中重新校验旧检查点和本批来源，原子写 text、through_sequence、through_run_id、revision、时间及 checksum。成功后释放原页缓存，从新边界继续加载；旧消息不再重复入摘要或正常模型窗口。
11. 需要多批时重复第 7～10 步，每次独立提交；最多 max_compaction_steps 次。通过预算后才进入正常 Provider 请求超时，摘要调用不消耗 Agent 的正常 round，但自身有次数上限。
12. 模型/工具输出按原 record() 路径逐条落库。工具结果令当前 Run 变大时，下一次 prepare() 再检查；当前 Run 不可压缩，超限就追加正常停止记录并结束，不删除结果。

## 摘要输出的两种限额

`summary_tokens` 沿用旧配置名，但实际限制是摘要字符串经 JSON 序列化后的 UTF-8 字节数，包含引号与转义；`summary_output_tokens` 才是 Provider 的输出 token 限额，并包含 reasoning。`summary_request()` 将前者和留出四分之一余量的目标值放入 `summary_limits`，`SUMMARY_PROMPT` 明确要求按字节预算重写精简，避免把模型输出上限误当作可保存正文长度。这是宿主输出契约修复，不宣称已验证真实模型的摘要质量收益；旧评测快照保留，新提示单独记录于 `evals/prompts/compaction-byte-budget.json`。

`compact_step()` 在响应后仍独立校验，拒绝理由细分为 `invalid_summary_empty/tool_calls/credentials/too_large/input_usage/output_usage`。日志只记录固定原因、正文与序列化长度和预算/usage 数值，不记录摘要、历史或异常正文。Provider 超时单独返回 `compaction_timeout`。拒绝不截断正文、不提升预算、不自动重试，也不推进检查点；下一条明确输入仍需通过同样校验。`tests/test_compaction.py` 覆盖预算传递、中文与 JSON 转义的边界及各失败原因。

## SQLite 持久化与恢复

schema 3 追加两个表，不修改旧 messages：

| 表 | 字段/用途 |
| --- | --- |
| context_runs | run_id、conversation_id、首尾 sequence、message_count、payload_bytes；已结束可重放 Run 的派生范围/大小索引 |
| context_summaries | conversation_id 主键、text、through_sequence、through_run_id、revision、updated_at、checksum；每会话一份当前摘要 |

迁移在同一事务中从已有 completed/failed Run 回填 context_runs。之后 context_run_finished 触发器随 finish_run 的终态事务插入范围元数据。联合索引支持按 Conversation/sequence 取有限 Run，message_run_order 支持只取已选 Run 的消息，不依赖每轮全表分组。

`storage/context.py::page()` 先读取 max_runs+1 条元数据，检查字节和消息数，再取选中的完整消息、反序列化并验证配对。单页最多 2 MiB、每 Run 最多 2048 条消息；实际页字节限额还受预算约束。过大首 Run 明确报错，不反序列化后再截断。save_summary() 仅对这批有界来源重新计算比较，不重新检查所有已覆盖历史。

提交验证旧 Summary 与调用者读取值一致、本批严格是下一段可重放前缀、源 Run/消息哈希未变；摘要正文和边界/checksum 在同一个事务内更新。读取校验摘要及其完整 Run 边界；checksum 是错误检测，不是认证。当前模型假设日志通过应用追加，不能对用户直接任意改写/删除旧 SQLite 内容提供持续完整性验证。

正常启动/恢复只读取摘要；准备请求验证实际选中的有界 Run。`ContextBuilder.history` 返回摘要边界后的有界工作页，不再承诺全部会话；有更多历史时需 prepare() 处理后才可发送。commit() 不复制整段消息链到第二份历史缓存。`load_messages()` 仍保留完整审计 API，Recall 仍查询原始消息。`SessionManager.status()` 只取 SQL 统计，不反序列化全部 RunResult。

## 失败、取消及已知限制

- ContextError 映射为 context_error 或 compaction_error；追加明确停止说明，完成 failed Run。Provider/超时/Memory 错误保持原语义。
- 摘要数据库失败经 HistoryError 进入 storage_error，当前 Run 标记 interrupted。不会把它伪装为成功摘要或普通 Provider 错误。
- 失败批次不保存摘要、不推进边界；之前成功的独立批次仍有效。取消/进程退出保留最后已提交检查点，遗留当前 Run 在重启时按原规则 interrupted，不自动执行工具或续跑任务。
- 本轮处理次数耗尽时停止，已提交进度保留；下一次明确请求从该边界继续，不无限重试。阈值处的摘要失败也选择停止，即使原上下文可能还在硬上限内，避免隐含回退策略。
- 没有可压缩前缀且硬超限，或单个历史 Run 仍无法放入摘要预算时明确失败。没有 Run 内递归拆分或任意用户消息截断；用户需要调整预算、近期保留数或新建会话。
- LLM 摘要有损，可能遗漏或错误提炼，必须保留不确定性并可用 Recall 核实。有限脱敏不是通用秘密识别器，不保证检测所有自然语言秘密；低信任数据也不保证模型绝不受注入影响，程序权限检查独立保持。
- SQLite/估算与证据投影仍是有界同步操作，模型请求超时不是磁盘硬取消。Compaction 没有跨文件事务或后台压缩，不触发 Reflection 或长期记忆更新。
- 为兼容已有短嵌入用法，budget=None 不猜模型窗口，也不执行压缩；只允许一页/262144 估算单位内的短模式，超过即 context_not_configured。生产嵌入应像 CLI 一样显式提供 ContextBudget；此短模式不提供模型窗口保证。

## 阅读、示例与验证

无真实 API 的最小嵌入路径为 `ContextBuilder("base", budget=ContextBudget(context_window=64000))` → `Agent(FakeProvider(...), ToolRegistry(), context)`。生产入口只需配置真实模型窗口，使用现有 Provider；手工管理命令 `/context status` 只显示统计，不触发摘要或模型请求，不创建 Run，也不显示摘要、消息或 reasoning 正文。

`tests/test_compaction.py` 覆盖短/长会话、预算、完整工具/Provider 重放、增量前缀、schema 2→3 回填/回滚、检查点冲突、空/超长/工具调用/凭证摘要、API/超时、数据库失败、真实进程退出和恢复、跨会话隔离、次数上限、SQL 范围及反序列化数量。`tests/test_config_cli.py` 验证显式窗口、组合预算配置错误和本地诊断；其余 Memory/Recall/Runtime 测试继续离线执行。

## V0.6 实际验证记录

本次离线完整回归为 350 passed、1 skipped（Windows 环境未具备创建 Symlink 的权限）；Ruff 检查、格式检查、uv build、两个 CLI 帮助入口均通过。已有 Windows Junction 隔离测试继续通过，网络禁用保护未改动。

构建出的 0.6.0 wheel 在独立临时目录连续启动两次，确认 schema 3、相同会话恢复，纯 /context status 不创建 Run 或模型调用。另以 60 个合成历史 Run 运行真实 CLI 交互，Provider 替换为离线 Fake：首次 7 批摘要、1 次正常回答，消息数 120→122；第二次启动复用原摘要边界 84，无新摘要调用，1 次正常回答后消息数 124。两次正常请求估算分别 45896、46075，均低于所配置输入上限 58880；没有真实 API 调用或费用。

V0.7 的 [Reflection](reflection.md) 是独立手动流程：从原始 completed Run 提出长期知识，用户批准后才写 Memory；其审阅边界与本模块 through_sequence 无关。Compaction 的工具序列、预算和恢复规则保持原实现。

## 取消后的提交屏障

compact_step() 在 Provider 返回后重新检查 deadline.expired() 和当前 task.cancelling()。即使自定义 Provider 吞掉 CancelledError，超时或前台取消也不发布晚到摘要；旧 checkpoint 与原 Journal 保留。tests/test_compaction.py::test_swallowed_cancellation_never_publishes_checkpoint 对两种情况分别验证。
