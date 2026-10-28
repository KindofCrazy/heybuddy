# 主 Agent 模型轮次与 Prompt 审计

## 职责与架构组成

`core/tracing.py:RunTrace` 定义可选的请求/返回记录 Protocol。`storage/tracing.py:SQLiteRunTrace` 在同一 SQLite Owner 内保存主 Agent 正常模型调用的输入快照、返回快照与用量。`ContextBuilder` 只持有 Protocol 和当前 Run ID，不依赖 SQLite；`SessionManager.context()` 为持久会话装配记录器，直接嵌入的无持久化 Context 默认不记录。

`model_rounds` 是 schema 14 的追加表，主键为 `(run_id, number)`，外键关联原 Run。它是调用当时的审计快照，不是长期 Memory、Conversation Recall 或可重放历史的来源。原 `messages` 继续保存完整内部消息及 Provider 数据。快照只保存通用内部消息、工具定义和 `max_output_tokens`，主动排除 opaque `provider_data`；不保存网络请求头、密钥、供应商 wire 格式或推理正文。

## 代码实际运行路径

1. `runtime.open_application()` 取得原数据库占用锁后调用 `SQLiteStore.initialize()`，在原迁移事务内追加 `model_rounds`。旧 Run 不回填推测的 Prompt。
2. CLI/QQ 的 `ChatService.run()` 进入 `Agent.run()`，`ContextBuilder.begin()` 创建 Run 和用户消息；每轮 `prepare()` 按原流程完成最新记忆、人格、时间、检索、历史与 Compaction 的编译和预算检查。
3. `Agent._generate()` 在真正的 `Provider.generate()` 前同步调用 `trace.start()`，使用当前 `ContextBuilder.run_id` 和从 1 开始的调用序号，将输入投影与当轮工具定义保存。投影序列化发生在 Provider 读取/修改输入前，不在之后用当前 Memory 重建。
4. Provider 在原超时范围内运行；正常返回后 `trace.finish()` 记录 responded、结束时间、耗时及供应商实际报告的 input/output/reasoning token 数。无 usage 时保留空值，不用估算替代。reasoning token 是 output 的子集，页面不重复相加。
5. Provider 失败、超时、意外异常或取消分别记录固定枚举。响应快照保存后，Agent 才沿既有验证、助手消息落库、工具顺序执行和完成提交路径继续；responded 只表示收到响应，Run 是否成功仍由原 Run 状态表示。无效响应中的工具也只是审计，不执行。
6. Observatory 的 `/api/runs/{id}/rounds` 先验证 Agent 归属，再按调用序号有界分页读取元数据。点击输入/返回折叠栏才请求 `/api/runs/{id}/rounds/{number}/payload?part=prompt|response&offset=...`。一次仅读取选中的有界快照，先对完整 JSON 脱敏，再分为每页 12000 字符，避免凭据在分页边界被拆开后漏过。前端用 textContent 显示。

## 异常与边界

单份输入或返回快照最多 2 MiB；超过时保存 `too_large` 和本轮元数据，不存半份 JSON，不改变实际发送给 Provider 的输入。输入审计写入失败时不调用模型；返回审计写入失败时按 HistoryError 停止 Run，不继续执行本轮工具。取消时若审计写入失败，记录固定诊断并继续传播原取消。日志不包含私人 Prompt 或异常正文。

进程硬退出可能留下 requesting；Run 恢复为 interrupted 后，查询投影显示 interrupted，不重发请求。独立只读 web 不执行该恢复、不修改审计记录。未完成返回、旧 Run 或准备上下文前失败可能没有快照，页面如实显示无记录。模型轮次只涵盖主 Agent 正常调用，不混入 Compaction、Organizer、Learning、Curator 或角色维护的额外请求。

保存的是实际传给通用 Provider 接口的输入投影；完整供应商 wire、opaque reasoning replay 未包含，不能当作原始 HTTP 包。HTTP 展示仍受脱敏及深度限制，审计与主数据库同为私人明文，沿用现有 Git 忽略策略，不提交合成以外的用户数据。

## 阅读与验证

离线最小示例运行 `uv run pytest tests/test_model_rounds.py -q`。测试通过 SessionManager、Agent、FakeProvider 和原生工具执行一次两轮加法，逐轮比较快照与 FakeProvider 实际收到的内部消息/工具定义，并验证末轮工具为空、tool call_id、token 用量、正文分页、失败/取消、写库失败阻止请求、跨 Agent 隔离、超限和 schema 13→14 原子迁移。页面入口为运行详情的“模型轮次与 Prompt”，支持完整宽度、刷新、返回与中英文切换，无真实模型调用。
