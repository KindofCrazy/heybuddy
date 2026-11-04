# Tool Use：从调用到可信执行结果

## 职责与架构组成

`src/personal_agent/tools/registry.py` 定义 ToolArguments、Tool、ToolRegistry 和 ToolExecutionError。ToolDefinition 是提供给模型的名称/描述/Schema，ToolCall 是模型提出的调用，ToolResult 是宿主执行回执。模型提出调用不等于已经执行。

## 代码实际运行路径

1. runtime.assemble_chat 注册内建工具、Memory 和 Recall；Harness 再注册工作区、规划、技能、子执行、后台与 Workflow。MCP.start 只导入 Host allowed_tools。
2. register 检查名称格式和唯一性。同步函数必须明确 side_effect_free=True；有写副作用的工具使用 async 包装，在事件循环中完成短提交，不能丢给超时后继续运行的线程。
3. Agent 每轮 definitions 过滤 required_permission，最后一轮为空。合法响应的完整 AssistantMessage 在执行前落库。
4. execute 依次检查注册名、逐 Run 权限、参数 JSON。ToolArguments 采用 Pydantic strict、extra=forbid，拒绝类型强转和非有限值。原 arguments 保持字符串，非法 JSON 也能产生关联调用的错误回执。
5. 取得 CURRENT_SCOPE，reserve_tool 预留根预算；再检查 Host policy，最后 emit PreToolUse。即使被 policy/Hook 拒绝，已经预留的额度也不退还。
6. 工具在自身 timeout_seconds 或 Registry 默认超时中执行。async 直接 await；无副作用同步函数通过 asyncio.to_thread。输出 BaseModel 转 JSON，对其他结果用 JsonValue 校验，再拒绝 NaN/Infinity。
7. 返回 ToolResult(call_id, name, ok, data, error)。Agent record ToolMessage 后才执行下一工具，并发 PostToolUse。下轮 prepare 可卸载大结果，但 Journal 仍完整。

## 错误与副作用

| 情况 | 结果 |
| --- | --- |
| 未注册、未授权、参数非法 | unknown_tool / permission_denied / invalid_arguments |
| 根额度、Host policy、Hook 拒绝 | execution_budget / policy_denied 或 policy_failed / hook_* |
| 工具超时、一般异常、非 JSON 输出 | tool_timeout / execution_error |
| 受控领域错误 | ToolExecutionError 的安全 code/message/data |
| Journal 保存失败 | 停止 Run，不继续下一工具 |

工具错误是数据，模型通常有机会调整策略。超时不代表副作用不存在：同步线程可能仍在运行，异步取消也不保证撤销外部操作。没有通用回滚或自动重试。日志仅记录工具名、call_id、错误类别，不打印参数和异常原体。

## 工具类别与边界

- Memory/Recall：长期理解和历史证据；写入需单独权限，详见 [Memory](memory.md)、[Recall](recall.md)。
- Workspace/command_run：仅工作文件与白名单操作，无任意 Shell 或生成代码执行，详见 [Execution](execution.md)。
- Skills：只读操作知识，详见 [Skills](skills.md)。MCP：Host 审核的外部工具，详见 [MCP](skills-mcp.md)。
- Todo/Task/Goal、delegate、execution_*、workflow_*：分别负责规划、深度一只读子执行、显式后台工作、注册流程，不因工具可见而开放任意任务平台。

## 阅读与验证

宿主注册的最小示例：

```python
class EchoArgs(ToolArguments):
    text: str


async def echo(args: EchoArgs) -> dict:
    return {"text": args.text}


registry.register(Tool("echo", "Return supplied text", EchoArgs, echo))
```

示例中的 ToolArguments、Tool、ToolRegistry 从 tools.registry 导入。`uv run pytest tests/test_tools.py tests/test_agent.py tests/test_harness.py` 覆盖 ID 关联、顺序、参数、超时、授权与共享预算。

## 未来改进

- 为工具补充只读/副作用/幂等/可核对类别，帮助呈现风险与规划，不自动授予权限。
- 将大结果、分页、部分成功与未知完成状态规范为统一结果契约。
- 明确直接 Registry 调用与 Agent 包装调用的审计事件差异，避免旁路遗漏。
