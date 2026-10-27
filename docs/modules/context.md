# ContextBuilder 与会话工作上下文

## 职责与架构组成

`core/context.py::ContextBuilder` 负责一次 Run 的原始消息生命周期和每轮模型工作视图。它依赖 `core/history.py::ConversationHistory`、CoreMemory、Provider Protocol，不导入 SQLite 或 SDK。`core/budget.py` 管理预算，`core/compaction.py` 生成增量摘要；具体 SQLite 查询位于 `storage/context.py`，由 `storage/history.py::SQLiteConversationHistory` 绑定 Conversation。

未传 history_store 时使用 InMemoryHistory，仍以完整 Run 和检查点划分窗口；不具备重启持久性。CLI 始终绑定 SQLite，并从 Settings 显式构造 ContextBudget。完整算法、配置和限制见[Compaction 模块](compaction.md)。

## 接口与数据视图

| ContextBuilder 操作 | History 接口 / SQLite 路径 | 含义 |
| --- | --- | --- |
| `_load()`、`history`、`build()` | summary() + page() → context_summary/context_page | 仅摘要边界后的有界工作页，不是全量审计 |
| `begin()` | begin(user) → begin_run() | 原子创建 Run 与用户消息，尚不加载旧历史 |
| `prepare()` | compact_through()、page()、save_summary() | 每轮预算检查，必要时按完整 Run 压缩旧前缀 |
| `record()` | append() → append_message() | 原消息/结果先提交，再加入 pending |
| `commit()` | finish() → finish_run() | 完成状态，清理 pending/页缓存，不复制整段历史 |
| `abort()` | interrupt() → interrupt_run() | cancelled/interrupted；保留审计，不自动续跑 |

完整审计仍调用 `SQLiteStore.load_messages()`，包括未配对的中断调用。正常模型工作页只包含 completed/failed 完整 Run；历史摘要与 pending 分开。history/pending 对外返回独立对象，不能通过修改返回值篡改存储。

`history` 现在只返回有界的未压缩页，有更多历史时仍需 prepare() 压缩后才能发正常模型请求；它不再承诺全部对话。`SQLiteConversationHistory.load()` 也走同一有界路径，一页放不下时明确要求准备上下文。不要把该接口当完整审计 API。

## 代码实际运行路径

1. `SessionManager.context()` 传入程序 DEFAULT_SYSTEM_PROMPT、绑定会话的 history adapter、共享 CoreMemory 和配置预算。
2. `Agent.run()` 调用 begin()，保存用户消息和 running Run，建立 pending，只返回 Base 与本次输入。
3. `_run()` 每次准备发模型请求时，先按本轮权限/终轮策略取得真实工具定义，再 await prepare()。
4. 首轮 `_load()` 读取已持久化摘要和边界后的有限完整 Run；后续同一 Run 复用页。Provider 数据和工具配对不被截断。
5. prepare() 以 Base、当前页和完整 pending 重新建立工作链；for_request() 每次读取最新 Core，再追加显式低信任 Summary JSON。原工作链、pending 和 Journal 不保存自动注入片段。
6. 估算包括本轮工具 Schema、Provider JSON 和输出余量。低于触发水位且没有待加载历史时直接返回；否则只对已结束、近期保护区之外的完整 Run 调 compact_step()。
7. 每批合法摘要原子保存后，释放旧页并从新边界继续；旧 Summary 与新压缩前缀合成新 Summary。达到每次准备的次数上限或不可缩减就明确停止。
8. 预算通过后 Agent 才在正常请求超时内调用 Provider.generate(max_output_tokens=...)。摘要超时独立，不被普通请求的超时包住。
9. 新 assistant/tool 消息通过 record() 逐条提交，下一轮再次检查。当前 Run 无论多长都不作为摘要源；超预算时保留已执行工具与结果并记录停止。
10. 完成时 commit() 只 finish() 并清理临时状态；取消/存储失败走 abort()。下次请求或重启按已提交摘要恢复，不读取全部旧消息。

`build()` 保留一个同步的有界基础视图入口，供检查和短场景使用；它不调用 LLM，也不替代生产请求的 prepare()。Provider 应收到 prepare() 返回的视图，不能绕过预算直接发送 build()。

## Summary、Memory 与权限

模型结构为 Base system → Core user JSON → Summary user JSON → 有界原始历史 → 当前 Run。Summary kind=conversation_summary，trust=historical_data_only，带 through_sequence/revision；它不伪装真实用户新请求，也没有系统优先级。自动摘要永不写 MemoryFS，不注册工具或授予权限；/new 共享 Core/Deferred，但没有前一个 Conversation 的 Summary。

Recall/Deferred 仍由模型按需调用，结果作为普通 ToolMessage 进入当前 Run 并保存。Context 不自动检索全库或加载全部 Markdown。每轮 Core 编译保持 8192 默认 UTF-8 总预算，错误仍是 memory_error。

## 异常与边界

ContextError → context_error/compaction_error；HistoryError → storage_error 并标记 interrupted；用户取消传播 CancelledError。写入检查点失败不推进失败批次边界，之前成功批次保留；不回滚文件副作用、不重试历史工具。SQLite 错误、正文、reasoning 和摘要不写日志。

budget=None 只兼容已有短嵌入模式，不猜模型窗口也不自动摘要；超过一页或短模式资源上限返回 context_not_configured。生产嵌入应传显式 ContextBudget。单个完整 Run 大于加载/摘要上限时不会任意拆开，需调整预算或新会话。摘要有损、估算非精确，详细 WHY 和限制见[Compaction](compaction.md)。

## 阅读与验证

最小路径：`ContextBuilder("base", budget=ContextBudget(context_window=64000))` → `Agent(provider, registry, context)`。CLI 使用 `uv run personal-agent`；`/context status` 只报告安全统计，不调用模型或创建 Run。

`tests/test_compaction.py` 用 Fake Provider/MockTransport 检查长短会话、窗口/输出预算、检查点、重启、增量边界、有界 SQL/解码和失败恢复；`tests/test_persistent_runtime.py` 保留持久化及取消语义；Memory/Recall 回归继续验证共享、取证与授权独立。运行 `uv run pytest`，不需要真实 Key。

## V0.7 Reflection 边界

ContextBuilder/prepare()/Compaction 算法不变。Reflection 通过独立 CLI/Service 读取原始 completed Run，不使用当前 Summary 作为事实，不推进 Compaction 边界。批准后的 Core 仍由下一次 for_request() 编译最新值，Deferred 仍按需读取；不注入候选或伪造用户审批消息。两条流程及测试见[Reflection](reflection.md)。

V0.7.1 的 /context status 还会显示服务端提供的 reasoning_tokens；它包含于 output_tokens，不能重复相加。未提供该字段时省略，输入仍使用原保守估算/预算流程。


## V0.7.2 Phase 1 集成

V0.7.2 的自动记忆检查发生在普通 Run 已完成后，不在 ContextBuilder 或 Tool Loop 每个 round 中执行。Worthiness/Reflection 计量独立于正常 Provider usage，Compaction checkpoint 不复用。Core 新写入通过后续原编译路径可见。
