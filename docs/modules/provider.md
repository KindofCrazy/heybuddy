# Provider 与 Responses 重放

## 架构组成

`llm/base.py::Provider` 是接受内部消息和 ToolDefinition、返回 ModelResponse 的 Protocol。 `llm/openai_provider.py::OpenAIProvider` 是唯一具体适配器，使用官方 AsyncOpenAI Responses API。 SQLite、MemoryFS、CLI 和会话选择都不进入 Provider；Agent 也不使用 SDK 响应对象。

## 实际运行路径

1. Agent 经 ContextBuilder.prepare/for_request 构造预算内的 Core、Summary 和完整 Run 窗口，连同本轮授权的工具定义传给 generate()。
2. `_input()` 转换 system/user 文本和 tool 输出；assistant 若有 provider_data['openai']，重放其原始 JSON 项目；否则使用归一化文本和调用生成输入。避免重复加入调用。
3. generate() 发起 store=False、truncation=disabled 请求，包含 reasoning.encrypted_content，并按可选 max_output_tokens 设置输出上限，然后等待 SDK。
4. 返回完整响应时提取文本、拒绝回答和 function_call；原始 output 转成普通 JSON，保留在 ModelResponse.provider_data 中；可用 usage 的 input_tokens/output_tokens 及可用 reasoning_tokens 转为内部 TokenUsage，不泄漏 SDK 对象。
5. ContextBuilder 经存储接口保存这些消息；重启后重新加载，Provider 继续走同一个 `_input()`，不需要专门的数据库响应转换逻辑。

## 异常与边界

SDK 超时映射 ProviderTimeout，网络和 HTTP 失败映射安全 ProviderError；不传播响应正文或请求体。不完整或不支持的输出明确报错。SDK 自动重试由 CLI 关闭。 Core Memory 作为普通 user JSON 文本映射，不提升为 system；工具授权由 Registry 检查。正常请求只发送 ContextBuilder 提供的有界工作视图，仍需重放的完整 Run 不丢 Provider 数据；改变模型/端点时仍需兼容对应的历史数据。

## 示例与验证

工具调用历史按 reasoning → function_call → function_call_output → 最终文本重放。 `uv run pytest tests/test_provider.py tests/test_persistent_runtime.py` 使用 MockTransport，验证真实 SDK 收到的恢复后输入与重启前预期一致，不访问真实 API。

V0.6 的 Provider Protocol 仅增加可选关键字 max_output_tokens；Compactor 复用同一接口并传空工具列表，返回调用时拒绝执行。摘要不会把旧 reasoning/密文作为模型输入，近期正常重放仍保留原 JSON。见 `tests/test_compaction.py::test_sdk_compaction_has_no_tools_and_recent_wire_pairs_survive`。输出上限含 reasoning，详见[OpenAI 官方说明](https://developers.openai.com/api/docs/guides/reasoning)；本地估算不能替代服务端 usage。

## V0.7.1 未完成响应诊断

generate() 先把 usage 归一化为内部 TokenUsage，再检查 response.status。非 completed 一律拒绝回放/执行，只在 ProviderError.diagnostics 中携带 ProviderDiagnostics：白名单状态、max_output_tokens/content_filter/unknown 原因以及数值用量；未知服务端原因归一为 unknown。不得传递 SDK error.message、未完成正文、reasoning 文本或其他原始响应数据。

TokenUsage.reasoning_tokens 可为空，兼容端点缺失 output_tokens_details；负数或非法计数按 malformed_response 安全拒绝。正常响应仍保留原 Provider JSON 重放契约。ReflectionError 保留阶段与这组安全数据，CLI 显示它们帮助区分额度耗尽和其他停止原因；普通请求的调用接口不扩张。验证见 tests/test_provider.py 的 incomplete 诊断、缺失 details、非法 usage 测试及 tests/test_reflection.py 的 CLI 失败传递。

## V0.7.2：分阶段计量与真实评测

正常 Agent 和 Compaction 继续使用原 Provider。回答后自动检查由 automation/meter.py::MeteredProvider 记录额外 calls/usage/耗时，复用同一接口，不把 SDK 对象引入 Core。独立 evaluation/runner.py::TraceProvider 用阶段 Prompt 标识 main/compaction/worthiness/extract/select/consolidate，调用前保守预留额度，返回后记录真实 usage；reasoning 是 output 子集。没有新增模型适配器或 SDK 自动重试。

真实评测必须显式 --live，只有固定合成数据，保存正常响应 trace 不导出原始推理；pytest 仍 Fake/MockTransport 无 API。匿名汇总的价格由显式 input/output 参数估算，不是账单。入口、失败与预算见 [Evaluation](evaluation.md)。

## 图片输入

`OpenAIProvider` 可接收 `ImageReader`。`_input()` 对携带 images 的 user 消息生成 input_text/input_image 内容块，先按 Host 全请求上限检查引用，再读取并校验 Agent 隔离的图片，在 wire 请求中编码为 JPEG data URL；纯文本格式不变。数据缺失/损坏或超限停止请求，不自动外网补图。`runtime.open_chat()` 装配原 SQLite 的读取器；工具/Provider 数据重放继续复用原路径。结构、预算、失败和测试见[图片输入](images.md)。
