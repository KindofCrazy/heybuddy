# HeyBuddy — V0.3 Editable Core Memory

一个运行在本机、核心 Harness 自行实现的 Python Personal Agent。在自建 Agent Loop、Provider、ToolRegistry、CLI 和 SQLite 会话上，加入可编辑、跨会话共享的 Markdown Core Memory。仓库：[KindofCrazy/heybuddy](https://github.com/KindofCrazy/heybuddy)。

## 当前范围与设计

持久 Agent 拥有多个独立 Conversation；每个 Conversation 保存完整 Message 与 Run。重启可继续上次会话，创建新会话则不会自动继承其他会话的聊天记录。借鉴 [Letta Code 的会话消息路由](https://github.com/letta-ai/letta-code/blob/main/src/agent/list-messages-routing.ts) 对 Agent 与 Conversation 的分离，以及 Markdown Core Memory 的思路，不复制其复杂后端。 **Conversation History 与 Core Memory 独立**：前者按会话隔离，后者归属 Agent、每次请求自动编译。记忆只在本轮显式授权后通过受限工具修改，不自动从聊天提取，不检索其他会话。

不实现自动记忆提取、Deferred Memory 检索、向量/Embeddings、Reflection、Context Compaction、 Telegram/QQ/Gmail、后台 Worker 或 Scheduler，也不自动续跑中断任务。

## 安装与配置

需要 [uv](https://docs.astral.sh/uv/)，Python 3.13；在项目根目录运行：

```powershell
uv python install 3.13
uv sync --locked
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

编辑当前目录的 `.env`，已有配置无需覆盖，数据库路径可省略：

```dotenv
OPENAI_API_KEY=your-key
OPENAI_MODEL=your-responses-api-model
# 使用 DeepSeek 时可设置以下地址及模型 deepseek-flash
# OPENAI_BASE_URL=https://api.deepseek.com
AGENT_DATABASE_PATH=.heybuddy/heybuddy.sqlite3
# 留空则为数据库路径加 .memory，再以 Agent ID 隔离
# AGENT_MEMORY_ROOT=.heybuddy/core-memory
AGENT_CORE_MEMORY_MAX_BYTES=8192
AGENT_MAX_ROUNDS=10
AGENT_REQUEST_TIMEOUT_SECONDS=60
AGENT_TOOL_TIMEOUT_SECONDS=10
AGENT_LOG_LEVEL=INFO
```

Key 和模型名必填，其余均有默认值。系统环境变量优先于当前目录 `.env`，不搜索父目录。自定义 API Base URL 要求 HTTPS；HTTP 只允许 `localhost`（可带结尾的点）、IPv4 `127.0.0.0/8` 和 IPv6 `::1`。不经 DNS 判断，不放行局域网地址或 `0.0.0.0` / `::`。默认使用 OpenAI 地址；DeepSeek 支持同一 SDK 的 Responses API，见 [DeepSeek 官方说明](https://api-docs.deepseek.com/guides/responses_api/)。

数据库路径相对启动目录解析；要恢复同一份数据，请保持相同工作目录或设置固定绝对路径。 `.env`、默认数据目录、SQLite 数据库及附属文件都被 Git 忽略。数据库与记忆目录含私人数据，均为明文，没有加密或自动备份。默认数据目录及常见数据库派生记忆目录已忽略；自定义 AGENT_MEMORY_ROOT 时放在仓库外或自行加入 .gitignore，不要提交它。记忆大小上限包含三个块与 JSON 编译开销，按 UTF-8 字节计算；配置范围 512～1048576，超限拒绝而不截断。

## 启动与会话命令

```powershell
uv run personal-agent
# 等价入口
uv run python -m personal_agent.main
# 帮助不读取密钥、不打开数据库
uv run personal-agent --help
```

| 输入 | 行为 |
| --- | --- |
| 普通文本 | 在当前 Conversation 中运行 Agent |
| `/new` | 在同一 Agent 下新建并切换到空会话 |
| `/sessions` | 列出会话 ID、更新时间，并标记当前会话 |
| `/resume <conversation_id>` | 根据完整 ID 恢复已有会话 |
| `/memory read <请求>` | 本轮允许读取记忆和版本历史工具 |
| `/memory edit <请求>` | 本轮允许读取、更新和恢复记忆工具 |
| `/status` | 查看 Agent、当前 Conversation、Run 状态和中断提示 |
| `exit` / `quit` / EOF | 正常退出 |

首次启动创建 Agent `heybuddy` 和一个 Conversation；之后自动恢复上次选中的会话。会话管理命令不送给模型或保存聊天消息；记忆授权命令连同请求作为用户消息进入 Run 并保存。授权只对当前 Run 有效，普通自然语言“记住”不会获得写权限，历史里的授权不会延续。无效 ID 或命令不会切换当前会话。 Ctrl+C 取消当前 Run 并退出。每个数据库同时只支持一个本地 CLI 实例，另一个实例会明确报错。配置错误退出码 2，存储/记忆启动错误退出码 1；单轮错误显示状态后仍可接收后续输入。

例如第一次输入“我正在开发 HeyBuddy，准备参考 Letta Code 设计长期记忆”，然后退出。第二次使用同一数据库启动并问“我们之前准备怎样实现 Memory？”，Provider 会收到恢复的前文。输入 `/new` 后再提问，则包含程序 Base System Prompt、共享 Core Memory 与新输入；可通过 `/resume` 返回旧会话。

## 使用可编辑记忆

```text
你 > /memory edit 请记住：我正在开发 HeyBuddy，希望回答使用简洁中文。写入 human.md。
Agent > （模型调用 memory_update 后确认；成功必须以工具返回为准）
你 > /new
你 > 我正在做什么项目？我喜欢怎样的回答？
Agent > （新会话可使用共享 Core Memory，无需加载旧会话）
你 > /memory read 请读取 human.md，并列出最近的记忆版本。
你 > /memory edit 请先查看历史，将记忆恢复到版本 <完整版本 ID>。
```

重启后同样加载已提交记忆。每次请求编译最新版本，允许工具的 Run 内更新也会在下一轮生效。读取和版本工具为 memory_read / memory_history，更新和恢复为 memory_update / memory_restore。更新必须携带最新 expected_version；连续更新两个块时，第二次需要第一次返回的新版本。恢复创建新版本，不删除历史。不要在记忆中存放 API Key 或其他凭证。

磁盘布局是 `<memory_root>/<agent_id>/versions/<version>/{persona.md,human.md,MEMORY.md,revision.json}`， CURRENT 原子选择当前版本。版本目录不可直接编辑，改动会破坏哈希校验；本地维护可使用 `MemoryStore.update(block, content, expected_version=...)`，详见[模块文档](docs/modules/memory.md)。 Base System Prompt 由程序维护，Core Memory 以 user 角色 JSON 数据注入，没有修改系统规则和工具权限的接口。

## 架构与实际运行路径

详细设计见[整体架构与 Pipeline](docs/architecture.md)，包括模块依赖图、请求时序图、四种历史视图、事务边界、Run 状态转换及启动/切换/恢复流程。[开发文档索引](docs/README.md)提供阅读顺序。

```text
main → SQLiteStore.initialize / recover_incomplete_runs → SessionManager
     → ContextBuilder(ConversationHistory) → Agent.run
          ↓ begin：保存 running Run + 用户消息
       ContextBuilder.for_request → 编译最新 Core Memory（不写入聊天历史）
       Provider.generate（内部消息 / 本轮授权的工具定义）
          ↓ record：先保存模型调用
       ToolRegistry.execute → 保存结果 → 下一轮模型请求
          ↓ commit：完成 Run 状态
       RunResult → CLI
```

- Agent 核心依赖历史 Protocol，不依赖 sqlite3、CLI 或 SDK 的具体响应对象。
- SQLite 保存 Agent / Conversation / Message / Run，client_state 记录上次选择。
- Message 保留完整 role、文本、工具参数、call_id、结果和 provider_data，不只保存展示文本。
- `user_version` 管理 schema；当前应用 0.3.0 仍对应 schema 1。CLI 每次启动显式初始化并按序迁移，迁移失败回滚，遇到更高版本或陌生数据库不覆盖。
- ContextBuilder 区分完整审计、可重放历史、当前上下文和运行中消息；目前不压缩上下文。
- Provider 继续使用 Responses API 的完整本地上下文、`store=False` 和 reasoning 项目重放，持久化前后使用相同映射。工具调用格式参考 [OpenAI 官方函数调用文档](https://developers.openai.com/api/docs/guides/function-calling)。

模块文档均包含架构组成、真实入口/执行顺序、异常边界和验证方法：

- [整体架构与端到端 Pipeline](docs/architecture.md)
- [Agent Loop](docs/modules/agent.md)
- [ContextBuilder 与历史边界](docs/modules/context.md)
- [内部及持久化数据模型](docs/modules/models.md)
- [Provider 与 Responses 重放](docs/modules/provider.md)
- [工具注册与超时契约](docs/modules/tools.md)
- [Core Memory、版本及权限](docs/modules/memory.md)
- [SQLite、迁移及事务](docs/modules/storage.md)
- [会话管理与 CLI](docs/modules/sessions-cli.md)
- [配置与 API 地址校验](docs/modules/config.md)

## 一致性、失败与中断

一次 round 是一次模型请求，SDK 自动重试关闭。最后一轮不提供工具定义；若模型仍请求工具，则不执行，逐个保存关联调用 ID 的 `round_budget_exhausted` 错误，返回 `max_rounds`。同一响应中的工具按顺序执行，每个调用在执行前持久化，每个结果在执行下个工具前持久化。

Run 与初始用户消息原子创建；消息使用会话内递增 sequence 排序。每次追加消息是短事务；正常结束时校验无悬空调用，原子保存终态、结束时间和 RunResult。只有成功完成保存后才向 CLI 报告最终成功。保存失败停止运行，已提交的历史不被覆盖。

| Run 状态 | 含义 | 正常模型上下文 |
| --- | --- | --- |
| `running` | 正在处理，可能已有部分日志 | 不包含 |
| `completed` | 已保存完整成功结果 | 包含 |
| `failed` | 模型/API/轮数等正常停止，已有完整停止记录 | 包含 |
| `cancelled` | 用户取消，可能存在未配对工具调用 | 不包含 |
| `interrupted` | 存储故障或启动时发现遗留 running | 不包含 |

完整历史 `SQLiteStore.load_messages()` 始终保留未完成记录；恢复时不伪造结果、不自动续跑或重试。中断/取消的整个 Run 被排除出模型上下文，`/status` 会提示。工具可能已经产生副作用；工具完成与结果落库之间仍有不可消除的崩溃窗口，SQLite 不能回滚外部效果。如果连标记中断也失败，Run 保持 running，下一次启动在占用锁下标记为 interrupted。

请求前记忆读取损坏或超限返回 `memory_error`，不会发送该次模型请求。 RunResult 的 `storage_error` 表示持久化失败，不代表工具未执行；其他停止状态与 V0.1 保持一致。日志不输出用户消息、工具参数/结果、Provider 原始错误正文或凭证。

## 注册新工具

```python
from personal_agent.tools.registry import Tool, ToolArguments
from personal_agent.tools.builtin import create_builtin_registry


class MultiplyArguments(ToolArguments):
    a: float
    b: float


def multiply(args: MultiplyArguments) -> dict[str, float]:
    return {"product": args.a * args.b}


registry = create_builtin_registry()
registry.register(
    Tool(
        "multiply",
        "Multiply two finite numbers.",
        MultiplyArguments,
        multiply,
        side_effect_free=True,
    )
)
```

同步工具必须显式声明 `side_effect_free=True`；Runtime 信任开发者承诺，不能自动验证函数行为。参数严格校验，结果须为 JSON 值或 Pydantic 模型。未知工具、非法参数和执行异常返回标准错误。同步工具在线程中执行，超时只停止等待，线程仍可能完成；Python 线程不能安全强制终止，程序退出可能等待线程。异步取消同样不保证外部副作用回滚；两者都不自动重试。禁止注册任意 shell、eval、exec 或执行模型生成代码的工具。

## 测试与检查

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

测试使用 Fake Provider、SDK MockTransport、临时 SQLite 和记忆目录，隔离真实配置并禁用外网，不需要真实 API Key，不消耗模型费用。覆盖重启恢复、会话隔离、完整消息/Provider JSON 重放、迁移回滚、数据库异常、批量工具调用中断、真实进程退出和多次重启，以及原有 Runtime 测试。 V0.3 另外覆盖跨会话记忆、更新冲突、失败回滚、大小限制、权限过期和 Base System Prompt 隔离。 Windows 沙箱临时目录受限时使用 `uv run pytest --basetemp=.pytest_cache/tmp`； asyncio 需要允许本地 socketpair，测试仍禁止外网。 uv 缓存目录不可写时设置 `$env:UV_CACHE_DIR = "$PWD/.uv-cache"`。

## 当前限制

V0.1 原有的进程内历史无法事后导入，V0.2 从首次启动时开始保存。 SQLite 操作同步执行，使用短事务和 2 秒繁忙等待，适合单人本机，不支持并发多进程服务。会话历史会完整加载并随请求发送，暂无 token 预算、压缩、跨会话检索、自动备份或数据加密。记忆工具具备按 Run 授权，通用外部工具的细粒度权限、幂等和任务恢复仍需后续设计。记忆提交在事件循环中同步执行，磁盘卡顿不能被 asyncio 超时中断；版本尚无自动清理。 Markdown 快照发布与 SQLite 结果保存没有共同事务：后者失败不能撤销已提交的记忆。原子指针不等于断电级持久性，当前无目录 fsync 或网络文件系统保证。固定路径与完整性校验不是 OS 沙箱；低优先级数据不代表模型绝不受恶意记忆文本影响。
