# Personal Agent — V0.1 Core Runtime

一个运行于本机的最小 Python Agent Runtime。核心 Harness 自行实现，便于阅读、测试和扩展。
当前只支持 CLI 会话、Responses API、显式注册工具和进程内对话历史。
目标仓库：<https://github.com/KindofCrazy/heybuddy>。

## 安装

需要 [uv](https://docs.astral.sh/uv/)。在项目根目录运行（以下命令适用于 PowerShell）：

```powershell
uv python install 3.13
uv sync --locked
# 仅在尚无 .env 时复制，避免覆盖自己的配置
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

项目固定 Python 3.13 系列，依赖版本由 `uv.lock` 锁定。`uv sync` 默认安装开发依赖。
若系统 uv 缓存目录不可写，可设置 `$env:UV_CACHE_DIR = "$PWD/.uv-cache"`。

## 配置与启动

编辑当前工作目录的 `.env`：

```dotenv
OPENAI_API_KEY=your-key
OPENAI_MODEL=your-responses-api-model
AGENT_MAX_ROUNDS=10
AGENT_REQUEST_TIMEOUT_SECONDS=60
AGENT_TOOL_TIMEOUT_SECONDS=10
AGENT_LOG_LEVEL=INFO
```

Key 和模型名必填；模型必须支持 Responses API 和函数调用，并对你的账户开放。
不在源码中指定模型，不自动探测模型或发送试探请求。其余变量可省略。
使用 DeepSeek 时，在 `.env` 中设置 `OPENAI_MODEL=deepseek-flash`、
`OPENAI_BASE_URL=https://api.deepseek.com`，Key 填对应服务的 Key。
省略 `OPENAI_BASE_URL` 时使用 OpenAI 默认地址；只支持 Responses 兼容端点，不回退到 Chat Completions。
DeepSeek 支持通过同一个官方 OpenAI SDK Provider 调用 Responses API，相关参数兼容性见
[DeepSeek 官方文档](https://api-docs.deepseek.com/guides/responses_api/)。
系统环境变量优先于 `.env`；只读取启动目录中的 `.env`，不向父目录搜索密钥。
`.env` 被 Git 忽略，`.env.example` 只包含占位值。不要在命令参数、日志或提交中写入凭证。

```powershell
uv run personal-agent
# 等价入口
uv run python -m personal_agent.main
# 查看帮助不需要密钥，也不产生网络请求
uv run personal-agent --help
```

可以输入“计算 12.5 加 7，再告诉我 UTC 时间”，连续聊天，输入 `exit` / `quit` 或 EOF 退出，
Ctrl+C 也可退出。标准输出显示回答，标准错误输出运行日志。
配置错误退出码为 2；单轮失败显示状态，CLI 仍可接收后续输入。

## 架构与执行流程

```text
CLI → Agent → ContextBuilder（system + 会话历史 + 新输入）
          ↓
       Provider.generate(messages, tool_definitions)
          ↓
       文本终答 → 提交历史 → RunResult
       工具调用 → Registry 校验/执行 → 按 call_id 写入结果 → 下一次模型请求
```

- `core/models.py`：Pydantic 内部消息、调用、结果、停止状态。不包含 OpenAI SDK 对象。
- `core/context.py`：构建上下文并保存当前进程内的会话历史。返回深拷贝，避免外部意外修改。
- `core/agent.py`：手写有界异步循环；同一 Agent 的多次调用按顺序运行。
- `llm/base.py`：一个结构化 Protocol 和两类 Provider 异常，无复杂继承体系。
- `llm/openai_provider.py`：唯一具体 Provider，集中转换 Responses API 输入输出。
- `tools/registry.py`：显式注册、严格参数校验、执行隔离和 JSON 结果规范化。
- `config.py` / `main.py`：配置与 CLI 装配；其他渠道将来可直接复用 `Agent.run()`。

一次 round 是一次模型请求，最多 `AGENT_MAX_ROUNDS` 次；SDK 自动重试关闭。
每次模型请求有 Runtime 超时，SDK 也使用同一请求超时。
同一响应的多个工具按模型给出的顺序执行，每个调用都会得到对应 ID 的成功或错误结果。
文本和工具调用同时返回时，先执行工具；仅没有工具调用且有文本时才视为最终回答。
拒绝回答作为文本展示；空响应、重复调用 ID、未完成响应都会明确失败。

停止状态：`completed`、`max_rounds`、`timeout`、`provider_error`、`invalid_response`、
`runtime_error`。工具错误包含 `unknown_tool`、`invalid_arguments`、`execution_error`、
`tool_timeout`，会返回模型而不是中断循环。
轮数耗尽时仍完成最后一次已接收的工具调用，再结束本轮，不发额外模型请求。
因此耗尽轮数的结果可能已有工具副作用；错误后保留工具记录及停止说明，不自动重试或回滚。
用户取消时取消当前异步调用、不提交该轮临时历史，已经发生的工具副作用仍然存在。

Responses 使用 `store=False`，每次发送本地完整上下文。Provider 将原始 output items
转换为普通 JSON，置于内部消息的 `provider_data["openai"]`，自行重放 reasoning、文本和工具调用；
核心 Loop 不解释这些数据。请求包含 `reasoning.encrypted_content` 以支持无服务端会话的推理模型。
工具 schema 来自 Pydantic；显式 `strict=False` 保留自定义工具的默认值/可选字段语义，
执行前始终由本地 Pydantic 严格校验。参考
[OpenAI 官方函数调用文档](https://developers.openai.com/api/docs/guides/function-calling)。

日志只记录轮次、工具名、调用 ID、状态和异常类型，不输出对话、参数、工具结果、请求体或密钥。

## 注册新工具

先定义 `ToolArguments` 子类和普通 Python 函数，然后在传给 Agent 的 registry 上注册：

```python
from personal_agent.tools.registry import Tool, ToolArguments, ToolRegistry


class MultiplyArguments(ToolArguments):
    a: float
    b: float


def multiply(args: MultiplyArguments) -> dict[str, float]:
    return {"product": args.a * args.b}


registry = ToolRegistry()
registry.register(Tool("multiply", "Multiply two finite numbers.", MultiplyArguments, multiply))
```

使用已有内置工具时，先 `registry = create_builtin_registry()` 再注册新工具。
`ToolArguments` 禁止额外字段、字符串到数字的隐式转换和非有限数字。
工具应返回 JSON 值或可转为 JSON 的 Pydantic 模型；无法规范化的返回值视为执行失败。
同步函数在线程中执行；异步函数直接 await，推荐将 I/O 工具写成可取消的异步函数。
超时只停止等待，不能强制杀死同步线程，也不能回滚副作用。当前内置工具都是短时无副作用操作。
未来引入外部写入工具时，需要单独设计权限、幂等和取消语义。
不注册任意 shell、`eval` 或 `exec` 工具，不执行模型生成的代码。

## 验证

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

若 Windows 沙箱临时目录不可写，可使用 `uv run pytest --basetemp=.pytest_cache/tmp`。
Windows asyncio 自身需要本地 socketpair；运行环境需允许回环连接，测试仍禁止外网连接。

测试使用 Fake Provider 和 SDK 的 HTTP MockTransport；会禁用真实网络连接。
不读取真实 `.env`，不需要 API Key，不调用模型、不消耗 API 费用。
覆盖直答、连续/批量工具调用、错误工具、参数/执行错误、超时/API 失败、轮数限制、ID 关联、
上下文连续性、OpenAI 请求/响应转换、reasoning 重放、配置脱敏和 CLI 退出。

## 当前边界与后续方向

当前没有流式输出、token 预算或上下文压缩；历史随会话增长，退出即丢失。
没有 SQLite、长期记忆、后台任务、checkpoint、自动恢复、Scheduler、Telegram/QQ 或 Gmail。
没有其他 Provider、多 Agent 架构、动态工具发现或框架运行时。

建议下一步先增加 Telegram 单渠道适配：验证 CLI 之外的入口能否直接复用 Runtime，
按用户/会话隔离 Agent 实例。之后再单独设计 SQLite 历史持久化，避免把通信和持久化同时引入。
