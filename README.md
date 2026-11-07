# HeyBuddy

一个本地运行、核心 Harness 自行实现的个人 Agent：Python 3.13、asyncio 手写循环、SQLite schema 24、普通 Markdown 长期记忆。当前支持共享执行预算、工具、Hooks、只读 Skills、MCP、规划、受控工作区、深度一只读子执行、后台作业、注册式 Workflow 和多层上下文压缩。角色库默认 none，当前开发配置选择 Luna。

## 先读这里

先读[机制与逻辑流程](docs/mechanisms.md)：用准备信息、执行动作、反馈循环和停止决策解释 Agent，适合向别人讲解，无需知道函数名。再读[一条消息在 Agent 内部怎样完成](docs/architecture.md)，对照实际代码理解来源、预算、上下文、工具、round 停止、Goal 验收和提交返回。

再按需查[完整模块导航](docs/README.md)。每个核心模块包含职责、机制流程、真实调用路径、异常边界、验证入口与未来改进；改进建议不代表已经实现。

| 关注点 | 文档 |
| --- | --- |
| 执行循环与停止条件 | [Agent](docs/modules/agent.md)、[Harness](docs/modules/harness.md)、[Planning](docs/modules/planning.md) |
| 逻辑会话与路由 | [Thread Router](docs/modules/thread-routing.md) |
| 模型输入与压缩 | [Context](docs/modules/context.md)、[Compaction](docs/modules/compaction.md)、[检索](docs/modules/retrieval.md) |
| 执行能力 | [Tool Use](docs/modules/tools.md)、[Hooks](docs/modules/hooks.md)、[Skills](docs/modules/skills.md)、[MCP](docs/modules/skills-mcp.md) |
| 长期理解与后台维护 | [Memory](docs/modules/memory.md)、[Learning](docs/modules/learning.md)、[Proactive](docs/modules/proactive.md) |
| 工作与恢复 | [Execution](docs/modules/execution.md)、[Workflow](docs/modules/workflows.md)、[存储](docs/modules/storage.md) |

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

Key、模型名和 AGENT_CONTEXT_WINDOW 必填，其余均有默认值。较小窗口需同时调整输出/摘要预算，组合不一致会明确报配置错误，见[配置说明](docs/modules/config.md)。系统环境变量优先于当前目录 `.env`，不搜索父目录。自定义 API Base URL 要求 HTTPS；HTTP 只允许 `localhost`（可带结尾的点）、IPv4 `127.0.0.0/8` 和 IPv6 `::1`。不经 DNS 判断，不放行局域网地址或 `0.0.0.0` / `::`。默认使用 OpenAI 地址；DeepSeek 支持同一 SDK 的 Responses API，见 [DeepSeek 官方说明](https://api-docs.deepseek.com/guides/responses_api/)。

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

## 当前边界

工具只执行宿主注册、校验和授权的操作，不开放任意 Shell、模型代码执行或配置管理。Main Agent 的 Memory 写工具按 Run 授权；P2 后台 Learning 使用独立 Host 策略，手动 Reflection 仍需审批。Markdown 是长期 Memory 唯一权威源，角色设定与相处记忆分离。

原始 Journal 完整保留，模型上下文可以使用派生摘要和工具产物引用。重启只标记中断，不自动重跑工具；Workflow 仅经可信显式 Resume 并核对副作用后恢复。关心式 Proactive 保持 Shadow；明确提醒是独立 Scheduler，可向已绑定 NapCat 主号投递，官方 QQ 仍仅被动回复。

## 运行与维护入口

- `/context status` 查看窗口与压缩，`/harness` 查看当前来源状态；`/compact` 可信显式压缩。
- `/goal 生成资料报告 | file:report.md` 建立带确定性检查的目标，文件存在不代表语义验收。
- Memory、会话、Reflection 等命令见[会话与 CLI](docs/modules/sessions-cli.md)和[配置](docs/modules/config.md)。
- [Observatory](docs/observatory.md) 只读查看本地状态；[NapCat](docs/napcat-setup.md)、[官方 QQ](docs/qq-setup.md)、[明确提醒](docs/reminder-delivery.md) 是外围接入说明。
- [旧 Memory 迁移](docs/memory-migration.md) 保留操作步骤；[历史归档](docs/archive/README.md) 保留阶段交付、评测与调研，不作为当前架构入口。

## 离线开发检查

新增 [HeyBuddy 三能力 Benchmark](docs/modules/heybuddy-benchmark.md)：Routing、Memory Learning、Proactive Decision 各 50 个独立快照，含五个公开来源的 35 个改编样本；提供真实生产链路适配器、确定性 Baseline、Shadow 隔离运行与逐题报告。数据尚未独立人工审核，离线接线验证不代表真实模型成绩。

Long-Horizon Phase 1 提供独立的合成离线规则路由评测，入口为 `uv run python -m personal_agent.evaluation.long_horizon --list`；数据格式、真实 Router 重放路径、指标与结果重算见 [Long-Horizon Benchmark](docs/modules/long-horizon-evaluation.md)。结果标记为 Offline Contract Test，不代表真实模型或 Memory Learning 能力。

[Thread Routing Benchmark v1 草案](docs/modules/long-horizon-benchmark-v1.md) 提供 12 场景/216 Turn、8/4 开发集与 Holdout 划分，新增澄清和历史证据关系指标；`--dataset evals/long_horizon_v1 --preflight` 可离线预估调用路径。Gold 明确标为 Agent 编写、待人工复核，当前不宣称正式模型能力结论。

[Thread Routing Benchmark v2 草案](docs/modules/long-horizon-benchmark-v2.md) 扩展到 30 场景/540 Turn、21/9 划分，保留旧 Holdout，增加碎片化/污染指标及真实 Episode→Thread→repair 状态维护路径。`--dataset evals/long_horizon_v2 --ablation all` 离线验证状态、任务身份 Prompt、few-shot 与弱规则的五组对照；固定 Provider 结果只代表接线和预算路径，不提供真实语义收益结论。

[v2 三场景真实模型 Pilot](docs/modules/long-horizon-pilot-v2.md) 提供独立 text/JSON profile，固定 LH019/LH021/LH022 与四组顺序，路由、Episode 和 Thread 共用人民币 10 元预算；`--pilot-preflight` 无模型预检，真实执行需显式 `--live`，九个摘要检查点在调用前冻结。两次尝试均保留失败记录，已审阅六份状态快照；四组尚未完整结束，不宣称 few-shot 收益。

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

测试保留网络禁用保护，使用 Fake Provider、MockTransport 与隔离数据；不调用付费模型。变更打包或入口时另运行 `uv build` 与 CLI 启动检查。不要把真实数据库、Memory、凭据或本地配置提交到 Git。

默认开启 Thread 在线路由：同一原始 Conversation 中可切换逻辑讨论，独立重放和压缩。`/new`、`/sessions`、`/resume <id>` 分别新建、列出、选择 Thread；`/thread new [title]|list|use <id>|correct <run_id> <thread_id>` 为可信 CLI 管理入口。关闭 `AGENT_THREAD_ROUTING_ENABLED` 可使用旧 Conversation 模式。语义模型选择默认关闭，开启方法与边界见 [Thread Router](docs/modules/thread-routing.md)。
