# Personal Agent 开发约束

## 目标与当前范围

本项目从零实现个人 Agent，V0.1 只提供可读、可测试、可扩展的进程内 Runtime、CLI、
OpenAI Responses Provider、Pydantic 工具注册及会话历史。用户需要掌握 Harness 的核心实现。
修改前检查目录、Git 状态和已有实现，保留用户工作。

## 架构

- `core/agent.py` 手写 asyncio Agent Loop；不得依赖 CLI 或 OpenAI SDK 的响应对象。
- `core/context.py` 独立构建上下文；当前只保存内存中的会话历史。
- `core/models.py` 定义统一内部消息和调用结果；Provider 专用数据仅允许普通 JSON。
- `llm/base.py` 保持简洁 Provider Protocol；仅 OpenAI adapter 了解 SDK 与 wire format。
- `tools/registry.py` 统一注册、校验、执行和结果规范化；未知工具必须拒绝。
- `main.py` 只负责配置、装配、交互和退出，不承载 Agent Loop。
- 工具错误作为关联 call_id 的数据返回模型；模型失败和轮数上限返回明确 RunResult。
- 不允许模型通过 eval、exec、任意 shell 命令执行生成代码。

## 技术与风格

Python 3.13、uv、Pydantic v2、asyncio、官方 OpenAI Python SDK、pytest/pytest-asyncio、Ruff。
不使用 LangChain、LangGraph、smolagents；不引入不必要依赖、工厂、依赖注入框架或复杂继承。
依赖更新后同步 `uv.lock`。保持类型标注、明确命名和小模块，Ruff 行长 100。
参数模型继承 ToolArguments；同步工具在线程中运行，优先使用可取消异步 I/O。
顺序执行同一响应的工具，保持可预测顺序；没有自动回滚或自动重试。
不要从其他路径搜寻凭证，不读取/输出真实 `.env` 内容。
Key/模型只从环境配置获得，密钥只能在被忽略的本地 `.env` 中，不能写进示例、日志或测试。
异常日志只记录安全诊断字段，禁止直接打印 SDK 异常体或 Pydantic 输入值。

## 测试与交付

核心测试必须离线、无真实 Key、无 API 费用；使用 Fake Provider 和 MockTransport。
保留 tests/conftest.py 中的网络禁用保护；不要调用真实 API 作为自动化验证。
修改后运行 `uv run pytest`、`uv run ruff check .`、`uv run ruff format --check .`。
涉及打包/入口时运行 `uv build` 和 CLI 启动检查。只汇报实际运行过的验证。
覆盖直答、连续与批量工具调用、未知工具、非法参数、执行异常、API/超时、轮数上限及 ID 关联。
测试应验证行为与契约，而不是机械重复实现。

## 未来边界

V0.1 不实现 Telegram/QQ、Gmail、SQLite、长期记忆、任务持久化、checkpoint、后台 Worker、
失败恢复、Scheduler 或主动对话。只有后续明确请求时才实现；保持 Provider、ContextBuilder、
Agent 和 CLI 职责分离即可，不预建这些模块。无微服务、Redis、消息队列、向量库、多 Agent。
