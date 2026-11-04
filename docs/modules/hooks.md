# Hooks：宿主生命周期扩展

## 职责与架构组成

`src/personal_agent/core/lifecycle.py` 定义不可变 LifecycleEvent 和 HookRegistry。Agent 与 ToolRegistry 共享 hooks。Hook 是宿主注册的 async callback，不是模型工具，也不运行 Skill 脚本；有 Registry 不等于已经安装自定义回调。

## 机制流程

1. 程序在执行开始、模型交互、工具使用和停止等关键节点发出事件，供宿主安装的扩展观察。
2. 观察者按顺序处理有限的事件信息，每个观察者和整个事件都有时间限制。
3. 工具真正执行前的检查可以进一步拒绝操作；其他节点用于观察，不能替换请求、结果或增加权限。
4. 执行前检查失败或超时就拒绝工具；普通观察失败只留下安全诊断，取消仍继续向外传播。

## 代码实际运行路径

| 事件 | 时机 | 返回值作用 |
| --- | --- | --- |
| RunStarted | begin/on_begin 后 | 观察 |
| BeforeModelCall | prepare 后、普通请求前 | 观察 |
| AfterModelCall | 普通响应成功、mark_consumed 后 | 观察 |
| PreToolUse | 参数/额度/policy 后、工具前 | 非 None 拒绝 |
| PostToolUse | Agent 将结果落库后 | 观察 |
| Stop | Agent.run finally | 观察 |

register 按顺序保存；emit 顺序 await，默认每 callback 1 秒、整个事件 2 秒。PreToolUse 的非 None 归一为 hook_denied，异常为 hook_failed，超时为 hook_timeout，交给 Registry 形成 ToolResult。其他事件异常只记安全诊断；取消仍传播。

BeforeModelCall 不覆盖全部 Provider 调用：摘要和评审可走其他路径，同 round 超限补救也不重新 emit。PostToolUse 由 Agent 发出，独立调用 Registry.execute 不会自动得到该事件。

## 异常与边界

事件仅有 ID、轮数、工具名和状态等元数据，无正文/可变参数。PreToolUse 的 round_number 当前为默认 0，不代表实际轮数。未知工具、无权限、非法参数在 Hook 前返回；Agent 仍会在结果落库后发 PostToolUse。Hook 只能进一步拒绝，不能授予权限。超时依赖协作式取消，不能强杀阻塞 Python。begin 失败时 Stop 可能无 run_id。

## 阅读与验证

```python
async def deny(event):
    return "deny" if event.tool_name == "example_write" else None


registry.hooks.register("PreToolUse", deny)
```

`uv run pytest tests/test_harness.py -k hook` 对照事件预算和拒绝；tests/test_agent.py、tests/test_tools.py 对照执行顺序。

## 未来改进

- 为拒绝/跳过路径建立明确事件覆盖契约，增加结构化拒绝原因。
- 限制回调数量并记录耗时，避免慢观察器挤占后续回调。
- 区分回答、摘要、评审的模型事件，统一诊断但保留各自语义。
