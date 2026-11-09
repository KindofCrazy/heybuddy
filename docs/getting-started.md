# 安装、配置与日常使用

[返回项目首页](../README.md) · [完整文档导航](README.md)

本文保留本地 CLI 的完整安装配置、数据存放与会话操作说明。首次使用可以先按[首页快速开始](../README.md#快速开始)完成最小配置。

## 安装与配置

需要 [uv](https://docs.astral.sh/uv/)，Python 3.13，且该 Python 的 SQLite 支持 FTS5 trigram；在项目根目录运行：

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
# 新安装默认用户数据目录；已有数据库旁的 .memory 目录继续原地使用
# AGENT_MEMORY_ROOT=D:/private-data/heybuddy-memory
AGENT_CORE_MEMORY_MAX_BYTES=8192
# 必填：设为所用模型真实支持的窗口 token 数，不按模型名/端点猜测
AGENT_CONTEXT_WINDOW=<实际窗口 token 数>
AGENT_RESERVED_OUTPUT_TOKENS=4096
AGENT_CONTEXT_SAFETY_MARGIN=1024
AGENT_COMPACTION_TRIGGER_RATIO=0.8
AGENT_RECENT_RUNS=2
AGENT_MAX_ROUNDS=10
AGENT_MAX_TOOL_CALLS_PER_RESPONSE=64
AGENT_MAX_TOOL_CALLS_PER_RUN=256
AGENT_REQUEST_TIMEOUT_SECONDS=60
AGENT_TOOL_TIMEOUT_SECONDS=10
AGENT_LOG_LEVEL=INFO
```

Key、模型名和 AGENT_CONTEXT_WINDOW 必填，其余均有默认值。较小窗口需同时调整输出/摘要预算，组合不一致会明确报配置错误，见[配置说明](modules/config.md)。系统环境变量优先于当前目录 `.env`，不搜索父目录。自定义 API Base URL 要求 HTTPS；HTTP 只允许 `localhost`（可带结尾的点）、IPv4 `127.0.0.0/8` 和 IPv6 `::1`。不经 DNS 判断，不放行局域网地址或 `0.0.0.0` / `::`。默认使用 OpenAI 地址；DeepSeek 支持同一 SDK 的 Responses API，见 [DeepSeek 官方说明](https://api-docs.deepseek.com/guides/responses_api/)。

数据库路径相对启动目录解析；要恢复同一份数据，请保持相同工作目录或设置固定绝对路径。 `.env`、默认数据目录、SQLite 数据库及附属文件都被 Git 忽略。数据库与记忆目录含私人数据，均为明文，没有加密或日常自动备份；旧格式迁移会创建一次性完整备份。新安装 Memory 默认在 Windows `%LOCALAPPDATA%/HeyBuddy/memory/`（其他系统 `~/.local/share/heybuddy/memory/`），并按 Agent ID 隔离；已有 `<database_path>.memory/<agent_id>/` 继续原地使用。默认数据目录及常见数据库派生记忆目录已忽略；自定义 AGENT_MEMORY_ROOT 推荐放在仓库外；仓库内实际 Agent 记忆目录必须整目录被 Git 忽略且没有已跟踪文件，否则初始化或写入会被拒绝。程序不会自动改写忽略规则或移除索引。记忆大小上限包含三个块与 JSON 编译开销，按 UTF-8 字节计算；配置范围 512～1048576，超限拒绝而不截断。

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
| 普通文本 | 在线选择 Thread 并串行运行 Agent，原文保留在 Conversation 日志 |
| `/new` | 新建并选择空 Thread；关闭路由时新建 Conversation |
| `/sessions` | 列出 Thread ID 和标题，标记当前 Thread；关闭路由时列出 Conversation |
| `/resume <id>` | 选择已有 Thread；关闭路由时选择 Conversation |
| `/memory read <请求>` | 本轮允许读取固定 Core 块和内容摘要 |
| `/memory edit <请求>` | 本轮允许 Core 与 Deferred 创建/编辑及索引补链 |
| `/memory check` | 本地检查缺失索引/链接、悬空/无效链接，不调用模型 |
| `/memory repair` | 用户直接授权本地补齐缺失索引/链接，保留原文，不调用模型 |
| `/status` | 查看 Agent、当前 Conversation、Run 状态和中断提示 |
| `/context status` | 本地查看预算估算、摘要边界/大小、加载页和最近压缩状态，不调用模型 |
| `exit` / `quit` / EOF | 正常退出 |

首次启动创建 Agent `heybuddy` 和一个 Conversation；之后自动恢复上次选中的会话。会话管理命令不送给模型或保存聊天消息；记忆授权命令连同请求作为用户消息进入 Run 并保存。授权只对当前 Run 有效，普通自然语言“记住”不会获得写权限，历史里的授权不会延续。无效 ID 或命令不会切换当前会话。 Ctrl+C 取消当前 Run 并退出。每个数据库同时只支持一个本地 CLI 实例，另一个实例会明确报错。配置错误退出码 2，存储/记忆启动错误退出码 1；单轮错误显示状态后仍可接收后续输入。

例如第一次输入“我正在开发 HeyBuddy，准备参考 Letta Code 设计长期记忆”，然后退出。第二次使用同一数据库启动并问“我们之前准备怎样实现 Memory？”，Provider 会收到恢复的前文。输入 `/new` 后再提问，则包含程序 Base System Prompt、共享 Core Memory 与新输入；可通过 `/resume` 返回旧会话。

## Thread 路由

默认开启 Thread 在线路由：同一原始 Conversation 中可切换逻辑讨论，独立重放和压缩。`/new`、`/sessions`、`/resume <id>` 分别新建、列出、选择 Thread；`/thread new [title]|list|use <id>|correct <run_id> <thread_id>` 为可信 CLI 管理入口。关闭 `AGENT_THREAD_ROUTING_ENABLED` 可使用旧 Conversation 模式。语义模型选择默认开启，仅在规则未决时调用；已有配置若显式设置 `AGENT_THREAD_ROUTER_MODEL_SELECTION=false`，仍保持关闭，可改为 true 启用。配置与预算边界见 [Thread Router](modules/thread-routing.md)。
