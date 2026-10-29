# 工具注册与执行

生产时间工具采用 create_builtin_registry(..., local_time=True)，与自动注入时间共用 core.temporal.temporal_context，返回本地 YYYY-MM-DD HH:MM:SS，不再让 UTC 工具覆盖已提供的本地时间。上下文时间够用时可直接使用，需要刷新再调用。默认兼容注册表保留 UTC 行为，不改变历史评测基线。tests/test_tools.py 校验同源本地输出，tests/test_qq_integration.py 校验实际生产工具描述。

生产 runtime.assemble_chat() 使用 create_builtin_registry(..., include_add_numbers=False)，不向主模型注册算术演示；register_memory_tools(..., unified_reads=True) 把主题搜索、路径读取、目录与索引检查收拢到 archival_memory_search。旧 Harness 和离线评测保留兼容注册表，避免改动冻结的工具调用基线。生产 CLI/QQ 默认四个工具、完整记忆授权时十个。

统一工具由 memory/archival.py::ArchivalMemoryArguments 严格校验互斥操作，再交给 memory/search.py::search_topics() 或现有 MemoryFS/Index 只读方法；搜索先沿索引建立有界目录，再读取有限正文，不新增知识副本、向量库或写权限。参数、搜索范围、分页与验证见 [Memory](memory.md) 的统一只读入口，以及 tests/test_archival_memory.py。

内置工具的自然语言描述使用中文，API 工具名、参数模型、参数名和执行函数保持不变；描述翻译不参与注册或权限判断。`tests/test_evaluation.py` 将当前主提示词和全部工具说明与中文快照核对，旧英文基线继续保留。

## 职责与架构组成

`src/personal_agent/tools/registry.py` 包含：

- `ToolArguments`：Pydantic 严格参数模型基类，禁止额外字段、隐式转换和非有限数字。
- `Tool`：名称、描述、参数模型、执行函数和开发者声明的 `side_effect_free` 契约、可选 required_permission。
- `ToolRegistry`：校验注册、导出定义、执行工具并生成标准 `ToolResult`。

`tools/builtin.py::create_builtin_registry()` 注册时间和加法两个无副作用同步工具，显式设置 `side_effect_free=True`。`core/agent.py::Agent._run()` 使用 Registry， Provider 只获得参数 Schema，不接触函数或注册元数据。

## 代码实际运行路径

1. 装配时通过 `ToolRegistry.register(tool)` 检查名称与重复注册。对非协程函数，还要求 `side_effect_free=True`；未声明直接拒绝注册。
2. `definitions(permissions=...)` 先过滤无授权的工具，再根据参数模型的 `model_json_schema()` 构建内部 `ToolDefinition`，交给 Provider 转成 API 工具描述。
3. Agent 在仍有后续模型预算时调用 `execute(call)`。Registry 只查找已注册名称，先复查本轮权限（未授权返回 permission_denied），再用 `model_validate_json(call.arguments)` 解析并校验原始 JSON。
4. 在 `asyncio.timeout(timeout_seconds)` 内执行：协程函数直接 await；同步函数通过 `asyncio.to_thread()` 执行，避免阻塞事件循环。同步函数若返回 awaitable，继续 await 该值。
5. Pydantic 返回模型转成 JSON 数据；`TypeAdapter(JsonValue)` 校验输出类型， `json.dumps(..., allow_nan=False)` 拒绝非有限数字。
6. 成功返回 `ok=True` 和数据；失败返回 `ok=False` 与明确错误码。所有结果保留原 `call_id`，由 Agent 加入上下文并关联到模型请求。

## 同步工具契约与异常边界

`side_effect_free=True` 是工具作者的显式承诺：该同步函数不执行文件写入、外部提交等副作用操作。 Runtime 在注册时要求此承诺，但不能自动证明函数的行为。同步外部写入工具不应通过此通道注册。该限制不是线程可取消的声明，也不是权限沙箱。

`tool_timeout` 仅表示调用方停止等待。线程可能继续运行甚至稍后完成；返回的错误明确说明完成状态未知、副作用没有被取消或回滚，不应自动重试。 Runtime 不将晚到的结果改写为本次调用成功。Python 线程不能被安全强制终止；退出程序时默认线程池的关闭也可能等待未完成线程。

异步工具依赖协作式取消，取消也不能回滚已发送的外部请求。超时机制不能保证终止阻塞事件循环或吞掉取消的协程。没有线程隔离进程、幂等存储、外部写入权限或自动恢复机制。未知名称返回 `unknown_tool`，参数错误返回 `invalid_arguments`，执行/输出错误返回 `execution_error`。日志不记录原参数、结果或异常正文。

## 记忆工具的特殊执行边界

`memory/tools.py::register_memory_tools()` 注册 Core read/edit/update，以及 Deferred list/read/create/edit/index_check/index_repair。Core 用 memory.read / memory.update；Deferred 读和检查默认可用，写与修复用 deferred.update。Registry 执行机制保持不变，同时过滤定义并复查本 Run 权限，旧 History/Restore 已移除。ToolExecutionError 将安全领域错误码转换为关联 call_id 的 ToolResult。可选 data 保留部分完成的结构化信息，也通过既有 JSON/非有限数校验；格式非法返回安全 execution_error，不输出原始值。

记忆工具是无 await 磁盘步骤的异步包装，不在线程中留下超时后继续执行的写入。磁盘读取、Git 隐私检查和 flush/fsync 会阻塞事件循环，超时不是硬截止。局部编辑使用 expected_hash 与唯一原文匹配；创建和元数据变更可能涉及多个文件，索引失败明确返回部分完成错误；deferred_edit 的 ok=false 可以同时携带 data.edited=true、新 hash、父索引路径和安全原因码，不自动回滚或重试。具体参数、字节预算及运行路径见[MemoryFS](memory.md)，对应 `tests/test_memory_runtime.py` 和 `tests/test_deferred_memory.py`。

## 阅读与验证

最小用法：`create_builtin_registry().execute(ToolCall(id="sum-1", name="add_numbers", arguments='{"a":2,"b":3}'))`，await 后得到关联 `sum-1` 的 `sum=5`。自定义同步函数注册时必须传入 `Tool(..., side_effect_free=True)`。

对应 `tests/test_tools.py`：

- `test_sync_tools_require_explicit_side_effect_free_contract` 验证缺少声明时注册失败。
- `test_sync_timeout_reports_unknown_completion_and_thread_can_finish_later` 使用事件控制真实线程，验证超时结果先返回、释放事件后线程仍能完成，并在 finally 中释放和等待线程，避免遗留工作。
- `test_async_tool_timeout` 验证协程超时仍返回标准错误。

运行：`uv run pytest tests/test_tools.py`，不调用真实 API。


## V0.4 默认可用的只读 Recall Tools

`recall/tools.py::register_recall_tools(registry, Recall(store, agent_id))` 注册 recall_search / recall_get，不设置 required_permission，因此普通对话可用。agent_id 是宿主绑定属性，两个参数模型都禁止额外字段；模型不能选择其他 Agent。函数只读旧历史，返回 Pydantic 证据模型，经 Registry 规范化为关联 call_id 的 JSON 结果。

RecallError 映射为安全的 ToolExecutionError，SQL 异常和查询正文不进入日志。搜索结果 trust 为 historical_data_only，既不注册工具也不改变 Memory 权限。SQLite 读取采用同步有界查询，async def 不保证硬超时；不创建后台线程、自动重建或重试。完整路径与限制见[Recall](recall.md)，测试见 tests/test_recall_runtime.py。

## V0.7.2 Phase 2：工具描述实验

builtin.py、memory/tools.py、recall/tools.py 的 description 明确使用时机、根目录空串、分页、原文/hash 与部分完成处理。参数模型、required_permission、函数及 Registry 定义过滤/执行复查保持原逻辑。描述帮助选工具，不能代替 Host 安全检查。

evaluation/prompts.py::tool_variant() 只在评测注册表用 dataclasses.replace 替换描述，不向模型提供注册接口。离线测试仍验证真实权限，评测另查工具尝试、文件不变和 Journal 保留。最终快照与生产描述一致；候选仍发生过一次无效根路径调用，不保证描述能消除全部无效调用。见 [A/B 结果](../prompt-ab-results.md)。
