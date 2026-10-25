# Provider 与 Responses 重放

## 架构组成

`llm/base.py::Provider` 是接受内部消息和 ToolDefinition、返回 ModelResponse 的 Protocol。 `llm/openai_provider.py::OpenAIProvider` 是唯一具体适配器，使用官方 AsyncOpenAI Responses API。 SQLite、MemoryStore、CLI 和会话选择都不进入 Provider；Agent 也不使用 SDK 响应对象。

## 实际运行路径

1. Agent 经 ContextBuilder.for_request 编译最新 Core Memory，连同本轮授权的工具定义传给 generate()。
2. `_input()` 转换 system/user 文本和 tool 输出；assistant 若有 provider_data['openai']，重放其原始 JSON 项目；否则使用归一化文本和调用生成输入。避免重复加入调用。
3. generate() 发起 store=False 请求，包含 reasoning.encrypted_content，并等待 SDK。
4. 返回完整响应时提取文本、拒绝回答和 function_call；原始 output 转成普通 JSON，保留在 ModelResponse.provider_data 中。
5. ContextBuilder 经存储接口保存这些消息；重启后重新加载，Provider 继续走同一个 `_input()`，不需要专门的数据库响应转换逻辑。

## 异常与边界

SDK 超时映射 ProviderTimeout，网络和 HTTP 失败映射安全 ProviderError；不传播响应正文或请求体。不完整或不支持的输出明确报错。SDK 自动重试由 CLI 关闭。 Core Memory 作为普通 user JSON 文本映射，不提升为 system；工具授权由 Registry 检查。完整本地会话会随请求发送，尚无 token 预算或压缩；改变模型/端点时仍需兼容对应的历史数据。

## 示例与验证

工具调用历史按 reasoning → function_call → function_call_output → 最终文本重放。 `uv run pytest tests/test_provider.py tests/test_persistent_runtime.py` 使用 MockTransport，验证真实 SDK 收到的恢复后输入与重启前预期一致，不访问真实 API。
