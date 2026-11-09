# Thread 在线路由

## 职责与架构组成

`organization/router.py::ThreadRouter` 在同一个串行 Agent 的每个新 Run 中选一次逻辑讨论。复用 `organization.models.Thread`，不创建 ContextRoute 或独立 Worker。Thread 可以先有 ID/标题而没有 current_state；后者由 Episode/Thread Organizer 用真实来源补齐。

`storage/routing.py::RoutingRepository` 保存来源范围、Run→Thread 绑定、当前选择及路由事件；`storage/thread_history.py::SQLiteThreadHistory` 实现既有 History 接口，写入仍委托原 Conversation Journal，读取按 Thread/Conversation/source 过滤。`storage/context.py` 提供共享的有界页读取及摘要 CAS。

Conversation 是原始来源日志，不再要求用户按它选择讨论。Thread 是逻辑讨论；Episode 是事后整理出的连续片段。Thread.current_state 是证据支持的最小主题状态，thread_context_summaries 是窗口压力产生的有损工作摘要，两者用途、更新时机和持久化入口不同。

## 代码实际运行路径

1. `open_application → assemble_chat → ChatService.run → HarnessRuntime.run` 绑定可信来源和根预算。Agent.run 使用现有锁串行执行，context.begin 原子创建 Run 与原用户消息，on_begin 保存 run_scope。
2. `ContextBuilder.prepare()` 首次调用 Host 安装的 prepare_scope，await ThreadRouter.route。先 bootstrap 旧日志归属：已有 Episode 的 Thread 优先，未整理 Run 进入明确标注的历史 Thread。不修改旧消息或猜测旧任务分类。
3. Router 读取当前 Thread、最近 24 个候选标题及有界状态，按 Host 手选、显式 Thread ID、唯一标题匹配、换话题、临近回复和闲聊规则选择。生产 Settings 默认开启 model_selection，模糊情况且有候选时最多发一次无工具模型请求，输入包括当前文本、当前 Thread ID、至多 12 张候选卡及 4 条邻近消息片段；调用计入根预算。
4. Repository.bind 校验当前 running Run、Agent 与 Conversation，事务保存绑定和事件。该 Run 后续工具轮次不再改变 Thread；CURRENT_THREAD 在 Run 退出时重置。
5. SQLiteThreadHistory 读取本 Thread 的独立摘要及其之后完整 completed/failed Run。ContextBuilder 将 active_thread、thread_working_summary 作为低信任 user JSON 插入 messages，历史 Run 按原 user/assistant/tool 消息序列追加，保留 call_id 和 Provider JSON；不将整个 Run 编码成一条 JSON 聊天，也不写回原 Journal。
6. 每轮仍执行原窗口估算和多层压缩。Thread 摘要只覆盖本 Thread 的完整 Run 前缀，允许原 Conversation sequence 存在其他 Thread 的间隔。旧 Conversation 混合摘要不会被注入。贡献器每轮刷新，Router 每 Run 只执行一次。
7. 空闲时 EpisodeOrganizer 从原 Run 整理，Thread 切换形成 required 边界，事务核查禁止跨 Thread 混段；ThreadOrganizer 收到 fixed_thread_assignments，并在提交前强制核查，不能把已路由来源搬到另一 Thread。

## 异常与边界

生产默认启用规则优先的模型辅助路由，Host 可通过 `AGENT_THREAD_ROUTER_MODEL_SELECTION=false` 关闭语义模型选择；已有显式 false 不会被新默认值覆盖。直接构造库级 `ThreadRouter` 仍默认关闭模型选择，离线评测需显式启用；不会宣称规则能理解所有隐式话题变化。最近候选之外的 Thread 可由可信 CLI 按 ID 选择。Agent 级 Memory、主题状态和只读 Recall 仍可共享；自动原始 Run 重放及工作摘要按来源隔离，切换来源不能继承工具授权。

启用模型时，若标题只是部分关键词命中，而其他干净候选的 current_state 又支持输入中的具体术语，Router 把这种竞争交给语义选择；完整标题明确出现或没有竞争状态时保留快捷规则。例如“machine slot”同时提到另一个训练 Thread 的 augmentation，不能仅因 Machine inventory 标题含 machine 就直接绑定。关闭模型时保留历史规则；此改动避免无差别增加所有历史回放的请求数，代价是缺少状态的歧义仍可能误判。验证见 `tests/test_thread_routing.py::test_partial_title_collision_uses_semantic_selection`。

开启 `model_selection` 后，短句长度本身不再直接判为当前任务续接；明确“好的/收到/明白”等 ACK 仍沿当前 Thread，不额外调用模型。既有“那/这个/怎么”等续接前缀在没有多个干净候选状态竞争时保留快捷规则，其余短句交给既有一次无工具选择，提示区分闲聊与任务推进，并在多个候选都能解释同一版本/截止日指代时澄清。关闭模型选择仍保留兼容规则；没有新增配置项或 Worker。前缀规则仍可能误判歧义，这是当前兼容边界；验证见 `tests/test_thread_routing.py::test_short_unrelated_message_uses_model_but_ack_does_not`。

`ThreadRouter(..., defer_weak_rules=True)` 是默认关闭的实验入口，必须同时开启 model_selection；它把换话题与临近续接两条弱规则交给既有模型选择，便于独立测量 Prompt 的作用。Host 选择、显式 ID、唯一标题及其他规则继续沿原路径处理。当前 Runtime 不开启此参数，也没有新增环境配置；[v2 离线对照](long-horizon-benchmark-v2.md) 通过真实 Router 使用它，不复制路由逻辑。

模型只返回受校验选择，不能执行工具、改权限或改 Host 配置。选择失败不重试；需要澄清时提示 Agent 追问、禁用本轮工具定义，Registry 再执行强制拒绝。取消向上传播。预算不足停止，数据库错误沿现有 HistoryError 路径停止，原工具不会被重跑。

模型选择返回非法 JSON、未知 Thread 或请求失败时，`selection_unavailable` 也设置 `metrics.ambiguous`，保持当前上下文用于追问，`HarnessRuntime.bind_context()` 将其传给 `ROUTING_CLARIFY`，Context 与 Registry 共同阻止工具。不能仅保留原 Thread 就当作已确认归属；验证见 `test_unavailable_route_blocks_tools_in_the_actual_runtime`。若多个干净候选状态都支持模糊指代，弱续接前缀也交给语义选择，明确 ACK 仍不增加调用。

可信 CLI 的 `/thread correct <run_id> <thread_id>` 只纠正同来源、已终结 Run，增加绑定修订与审计，事务失效原/新 Thread 摘要。若旧 Episode 因此包含多个归属，解除其 Thread 关联，原摘要和原 Run 保留，不冒充新归属的状态证据。此操作不自动调用模型或补做工具。

重启恢复当前 Thread 指针和已保存进度，不续跑 interrupted Run。关闭 AGENT_THREAD_ROUTING_ENABLED 恢复 Conversation 历史模式，保留新表。库级 SessionManager 不依赖 Router，仍支持独立 Conversation。

## 阅读与验证

CLI 输入 `/thread new ProjectAlpha`，正常交互后输入“另外，ProjectBeta”，再输入“继续 ProjectAlpha”。`/thread list` 查看当前指针；`/context status` 查看 Thread 和压缩信息。`/new`、`/sessions`、`/resume <id>` 是对应 Thread 命令的兼容拼写；后台 `/thread organize|repair|show` 保留原维护职责。

离线运行 `uv run pytest tests/test_thread_routing.py tests/test_qq_integration.py`，检查 A→B→A 的原始工具重放、摘要隔离、来源与重启、错误选择/取消、歧义拒绝、Episode 强制分段、固定归属发布、Host 纠正、CLI 和 schema 23→24 原子迁移。测试使用合成 Journal 和 Fake Provider，不代表真实模型路由准确率。
