# 工具注册与执行

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

`memory/tools.py` 注册五个固定工具，分别使用 memory.read / memory.update / memory.restore 权限；权限默认空集合，Agent 将本轮宿主授权同时传入 definitions 和 execute，不依赖模型自律。 ToolExecutionError 将安全领域错误码（如 memory_conflict）转换为标准 ToolResult。memory_edit 以唯一原文匹配保护局部修改，memory_update 保留显式整块替换；memory_restore 独立授权。记忆读取、Git 隐私检查和提交均是协程包装的同步 I/O，没有 await 点，不在线程中执行有副作用的写入。它会短暂阻塞事件循环，timeout 无法强制打断文件系统操作；并不改变一般同步工具的超时契约。具体授权、冲突和版本路径见[MemoryStore](memory.md)，测试见 tests/test_memory_runtime.py。

## 阅读与验证

最小用法：`create_builtin_registry().execute(ToolCall(id="sum-1", name="add_numbers", arguments='{"a":2,"b":3}'))`，await 后得到关联 `sum-1` 的 `sum=5`。自定义同步函数注册时必须传入 `Tool(..., side_effect_free=True)`。

对应 `tests/test_tools.py`：

- `test_sync_tools_require_explicit_side_effect_free_contract` 验证缺少声明时注册失败。
- `test_sync_timeout_reports_unknown_completion_and_thread_can_finish_later` 使用事件控制真实线程，验证超时结果先返回、释放事件后线程仍能完成，并在 finally 中释放和等待线程，避免遗留工作。
- `test_async_tool_timeout` 验证协程超时仍返回标准错误。

运行：`uv run pytest tests/test_tools.py`，不调用真实 API。
