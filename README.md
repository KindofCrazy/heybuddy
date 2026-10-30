# HeyBuddy — P3 Passive Character Agent

[P2 Review Fix](docs/p2-review-fix.md) 增加有界启动/Idle 回执核对，安全恢复部分 Memory 发布；Preflight 保留确定性基线，Curator 按目标路径查找真实来源。真实文件冲突继续写保护，不盲目重试、不修改 P3 人格或长期记忆权限。

[P3 真实 API 效果检查](docs/p3-live-validation.md) 使用 deepseek-flash 完成 E0/E1/E2 对比和真实 Episode/State smoke，记录用量、实际表现、预算问题与局限；没有把 FakeProvider 或单次小样本当成角色质量证明。

P1 默认自动恢复上次选中的内部 Conversation Stream，直接输入即可持续聊天；`/new`、`/sessions`、`/resume` 仅保留为开发者历史管理命令。每次请求加入本地带时区时间与上次互动间隔，跨午夜不清空上下文。旧流不合并或删除，仍可通过 Recall 查到。

新增 [Episode 事后整理](docs/modules/organization.md)：`/episode status|organize|list|show <id>`。默认空闲 25 分钟或累计 8 条后有界整理，新输入抢占。Episode 保存版本、时间、摘要、引文与原始 Run 引用，不写长期 Memory。

`/thread status|organize|repair|list|show <id>` 查看并维护跨时间话题。默认每四小时在 Idle 检查 Pending，累计 3 个 Episode 可提前；Dirty Thread 在下个 Idle 优先分页重建，无新 Episode 也可修复。一次性聊天可无 Thread，Episode 修订与 Thread 进度独立。

[Working Context Retrieval](docs/modules/retrieval.md) 在明确恢复历史话题时按预算提供 Thread state、最近 Episode 与少量完整原始 Run；普通紧邻接话走 fast path，不增加模型请求，未命中保留 Recall。

[P1 Review Fix 交付报告](docs/p1-review-fix.md)：时间驱动、闲置积压自动消化、Dirty 最终一致性、取消/重启、迁移与验证。维护轮默认最多 4 批/4 请求/131072 预留 tokens/120 秒，冷却 60 秒后继续；不会按时钟机械创建 Episode/Thread。

一个运行在本机、核心 Harness 自行实现的 Python Personal Agent。在自建 Agent Loop、Provider、ToolRegistry、CLI 和 SQLite 会话上，保留可编辑的 Markdown Core Memory，提供分层 Markdown Deferred Memory 与按需跨会话历史检索 Recall；V0.6 增加有界上下文和可跨重启恢复的会话压缩摘要，V0.7 提供 Reflection，V0.7.2 在答案后自动筛查并生成可审阅提案，并增加独立的真实模型行为评测。仓库：[KindofCrazy/heybuddy](https://github.com/KindofCrazy/heybuddy)。

[P2 完整交付报告](docs/p2-delivery.md) 记录五个里程碑、实际架构、自动策略、恢复边界和完整离线验证。

## 当前范围与设计

[P3 Character Runtime](docs/modules/character.md) 与 [架构说明](docs/p3-character-architecture.md) 提供本地、只读、可替换的角色包；默认普通模式保持兼容，首个包为原创 BBC Sherlock 适配。

角色入口简化为 OpenPersona 风格的 `persona.json`：读取身份、性格、说话方式、背景和可选本地指南，再接上独立的 [通用交谈提示词](src/personal_agent/prompts/conversation.md)，生成一条系统消息。无需固定八个章节、情境示例或 Lore，不运行额外角色状态请求。完整装配、字段白名单和异常边界见 [简化角色模块](docs/modules/persona.md)；旧 Markdown 角色保留只读兼容，见 [Character 模块](docs/modules/character.md)。

主对话提示词与工具说明使用中文，保留人名、专业名词和程序标识；参见 [与 Miru 的逐段对照](docs/prompt-comparison-miru.md)，了解当前表达优势、冗余和改进顺序。

[当前记忆系统与模型工具](docs/memory-and-tools.md) 说明 persona.md 与 soul.md 的分工、记忆存储/检索/更新路径，以及默认工具和逐 Run 授权。

当前开发使用 [Luna persona.json](src/personal_agent/character/packages/luna/persona.json)，配置示例为 `AGENT_CHARACTER_ID=luna`。设定来自 [OpenPersona 的 ai-girlfriend 预设](https://github.com/acnlabs/OpenPersona/blob/main/presets/ai-girlfriend/persona.json)，已整理为中文并保留 MIT 声明，详见 [来源与适配](src/personal_agent/character/packages/luna/SOURCE.md)。Miru 角色卡已移除，旧对照文档仅作历史记录；Sherlock 包和评测保留。原文件中的语音、自拍、主动联系和工具授权不随角色启用。

[Proactive Shadow](docs/modules/proactive.md) 实现 P4.1/P4.2：从新 Run/Thread 发现有证据的候选，维护取消、完成和失效，并在有界后台进行 Wake/Attention 判断。默认开启，可用 `/proactive status|proposals|decisions|run|retry` 查看或检查；`AGENT_PROACTIVE_ENABLED=false` 可关闭。Observatory 新增 `/proactive` 只读页面。当前只记录建议，不发送主动消息或执行任务，首次开启不补学旧历史。

[OpenPersona 组装对照](docs/openpersona-prompt-assembly.md) 解释 injection.md 与 SKILL.md 的区别、完整 behavior-guide.md 怎样参与编译，以及当前身份/背景、表达、指南和通用聊天规则的顺序。

[角色对话自然度诊断](docs/character-dialogue-diagnosis.md) 对照 Miru/SillyTavern，说明自我分析、示例风格和时间冲突，并提供尚未替换生产配置的候选写法。

[Holmes 与 Miru 的逐段改写](docs/holmes-miru-prompt-rewrite.md) 记录已应用的角色改写、运行时段落编译和一次无历史真实调用；本轮结果仍有自我说明问题，不宣称自然度已改善。

[Miru 记忆架构](docs/miru-memory-system.md) 对照核心块、主题槽位、后台写入和检索工具；生产入口已移除 add_numbers 演示工具。

[P3 短期状态](docs/modules/character-state.md) 仅在角色 E2 启用后于 Idle 维护，按真实角色 Run 来源保存模拟 Mood/Focus，有独立有效期并共享 P1/P2 预算；不主动发消息。`/character status|run|retry` 查看或调试。

开发时在已有可信配置中设置 `AGENT_CHARACTER_ID=luna`，保持实际模型窗口配置；`none` 恢复普通模式。库级 Settings 默认仍为 none。persona.md 保存有真实用户证据的相处认识与反思，human.md 保存用户资料，角色 persona.json 保持稳定设定；旧的非空静态人格仍保留且不自动重新解释。Luna 不使用旧角色状态任务；旧 Markdown 角色仍可通过 `AGENT_CHARACTER_STATE_ENABLED=false` 关闭状态请求。修改角色或通用提示词后重启生效。

[P3 离线评测](docs/modules/character-evaluation.md)：`uv run python -m personal_agent.evaluation.character`，13 个情境含 20 轮漂移，E0/E1/E2 固定条件对比；默认无网络/费用。FakeProvider 只验证工程契约，真实角色一致性需后续显式生成和人工审阅。

[P2 Memory Learning](docs/modules/learning.md) 复用 Reflection 的来源验证与受限 MemoryFS Writer，按目标聚合候选，不改变手动 Reflection 审批和 Main Agent 工具权限。

[P2 Memory Curator](docs/modules/curator.md) 低频整理小组 Markdown，去重、证据核对和有限拆分共用同一 Writer；不删除文件、不修改 persona，目标和索引成功前保留原文。`/memory curator status|run|retry` 用于调试。

[P2 Adaptive Retrieval](docs/modules/retrieval.md) 沿 Memory Index 检索相关 Markdown，配合现有 Thread/Episode；短接话不增加模型调用，模糊历史问题可用一次有界只读 Preflight。`/context status` 查看预算和检索计量，`AGENT_MEMORY_RETRIEVAL_LLM_PREFLIGHT=false` 可关闭额外请求。

P2 自动 Learning 默认启用，接管旧的回答后 Automation；首次启动只记录水位，不静默重学历史。普通路径在 Idle 按 Finalized Episode 数量/六小时等待触发；明确记住/决定/更正可后台优先处理。`/memory learning status|run|retry` 查看和调试，`AGENT_MEMORY_LEARNING_ENABLED=false` 关闭 P2 并恢复原 Automation 配置。含不确定发布的任务先用 `reconcile <job_id>` 核对，不盲目重写。

持久 Agent 拥有多个独立 Conversation；每个 Conversation 保存完整 Message 与 Run。重启可继续上次会话，创建新会话则不会自动继承其他会话的聊天记录。借鉴 [Letta Code 的会话消息路由](https://github.com/letta-ai/letta-code/blob/main/src/agent/list-messages-routing.ts) 对 Agent 与 Conversation 的分离，以及 Markdown Core Memory 的思路，不复制其复杂后端。 **Conversation History 与 Core Memory 独立**：前者按会话隔离，后者归属 Agent、每次请求自动编译。Core Memory 可通过本轮显式授权的受限工具修改，或由用户批准具体 Reflection 计划后更新；P2 默认通过 Host 策略自动学习到受限 Markdown，关系候选可更新相处记忆，不修改角色包；关闭 P2 后可沿用原回答后提案与审批模式；Recall 可以在同一 Agent 的不同会话之间查找已结束的历史文本。

三层分工：**Core** 每次请求加载身份、稳定偏好和根索引；**Deferred** 保存按主题提炼的长期理解，由模型沿分层 MEMORY.md 按需读取；**Recall** 查询原始历史证据。普通 Markdown 是 Core/Deferred 的唯一权威源，可以直接在编辑器修改。没有 Git-backed Memory 或 Snapshot 版本系统。

P1 只增加本地进程内有界事后整理与输入抢占，不实现通用后台任务平台、向量/Embeddings、Telegram/Gmail 或工具续跑；当前 QQ 已通过独立官方被动渠道接入共享 Runtime。[P1 交付与迁移说明](docs/p1-delivery.md)包含实际架构、执行链路、验证、限制与 P2 接口；旧 Markdown 迁移另见[记忆迁移说明](docs/memory-migration.md)。

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
| 普通文本 | 在当前 Conversation 中运行 Agent |
| `/new` | 在同一 Agent 下新建并切换到空会话 |
| `/sessions` | 列出会话 ID、更新时间，并标记当前会话 |
| `/resume <conversation_id>` | 根据完整 ID 恢复已有会话 |
| `/memory read <请求>` | 本轮允许读取固定 Core 块和内容摘要 |
| `/memory edit <请求>` | 本轮允许 Core 与 Deferred 创建/编辑及索引补链 |
| `/memory check` | 本地检查缺失索引/链接、悬空/无效链接，不调用模型 |
| `/memory repair` | 用户直接授权本地补齐缺失索引/链接，保留原文，不调用模型 |
| `/status` | 查看 Agent、当前 Conversation、Run 状态和中断提示 |
| `/context status` | 本地查看预算估算、摘要边界/大小、加载页和最近压缩状态，不调用模型 |
| `exit` / `quit` / EOF | 正常退出 |

首次启动创建 Agent `heybuddy` 和一个 Conversation；之后自动恢复上次选中的会话。会话管理命令不送给模型或保存聊天消息；记忆授权命令连同请求作为用户消息进入 Run 并保存。授权只对当前 Run 有效，普通自然语言“记住”不会获得写权限，历史里的授权不会延续。无效 ID 或命令不会切换当前会话。 Ctrl+C 取消当前 Run 并退出。每个数据库同时只支持一个本地 CLI 实例，另一个实例会明确报错。配置错误退出码 2，存储/记忆启动错误退出码 1；单轮错误显示状态后仍可接收后续输入。

例如第一次输入“我正在开发 HeyBuddy，准备参考 Letta Code 设计长期记忆”，然后退出。第二次使用同一数据库启动并问“我们之前准备怎样实现 Memory？”，Provider 会收到恢复的前文。输入 `/new` 后再提问，则包含程序 Base System Prompt、共享 Core Memory 与新输入；可通过 `/resume` 返回旧会话。

## 使用长期记忆

```text
你 > /memory edit 请记住我偏好简洁中文，整合到 human.md 的已有原则。
你 > /memory edit 为 HeyBuddy 创建项目记忆，记录目前架构和已确认的设计决策。
Agent > （先查看索引，读取已有主题；无现有主题时调用 deferred_create 并补齐发现链）
你 > /new
你 > HeyBuddy 目前采用什么架构？
Agent > （从 Core 根索引 → projects/MEMORY.md → projects/heybuddy.md 按需读取）
你 > /memory edit 把已有项目记忆中的某条设计决策改为新决定，保留其他内容。
你 > /memory check
你 > /memory repair
```

Core 固定为 persona.md、human.md、MEMORY.md，每次请求重新编译，总容量默认 8192 UTF-8 字节（含 JSON）；不把 Deferred 正文全部拼入 Prompt。Root MEMORY.md 兼顾重要原则和顶层知识目录的用途/何时打开说明。Deferred 目录拥有自己的 MEMORY.md，普通主题文件有两个单行 Frontmatter 字段：

```markdown
---
name: heybuddy
description: HeyBuddy 的架构、设计决策和开放问题。
---

# HeyBuddy

当前采用自建 asyncio Harness 和 SQLite Conversation Recall。
```

目录与主题按内容组织，不按聊天日期建日志。所有索引使用相对 Agent Memory Root 的链接：根写 `- [Projects](projects/MEMORY.md): 项目架构与设计取舍。`，子索引写 `- [HeyBuddy](projects/heybuddy.md): HeyBuddy 的当前理解。`。新文件通过各级索引发现；name/description 用于有界目录列举，正文只在相关时读取。

Deferred 只读工具 deferred_list / deferred_read / deferred_index_check 默认可用；Core 读取工具 memory_read 使用 `/memory read` 或 `/memory edit` 授权。写入必须本轮显式 `/memory edit`：优先 memory_edit / deferred_edit 局部替换，显式整块替换才用 memory_update；deferred_create 创建时不覆盖旧文件并自动补链。读取返回 hash，写入携带 expected_hash；old_text 必须非空且精确唯一匹配。自然语言“记住”和旧会话里的授权不授予当前写权限。

普通文件可直接用 UTF-8 编辑器查看和修改，重新读取即生效；过期 hash 拒绝覆盖。受控写入有短锁和同目录原子替换，外部编辑器与 Agent 最后检查/替换之间仍有小竞争窗口，避免同时编辑同一文件。Deferred 文件最多 65536 字节，目录列举每页最多 10 条；读取返回 truncated/next_offset，可传上次 hash 继续读取。工具 data JSON 上限 16384 字节。

文件写入与索引更新是多个单文件操作，索引步骤失败返回 memory_index_incomplete，不声称整体成功。先 `/memory check`，再明确 `/memory repair` 补齐缺项；修复补齐缺项并刷新校验完整的生成条目，不删除用户的无效、重复或悬空链接，也不改写人工/无标记说明。检查有界全树和索引目标，各最多 500 项；分页问题详情也可经 deferred_index_check 继续查询。

新目录使用中性概要，不把首个主题的 description 当作整个目录用途；添加主题保留已有目录说明。用户了解目录范围后，可直接完善概要或通过 deferred_edit 修改，完整生成标记的父入口会随之更新。Frontmatter description 描述文件内容，父索引也可描述何时读取，两者不必相同。

V0.5.1 新生成条目有内联 heybuddy-index 校验注释。正文编辑不改父索引；name/description 变化只自动刷新仍保持生成原文的条目。旧无标记或人工改写条目会保留，并返回具体 index.path/reason 供 review。索引维护失败时工具 ok=false，但 data.edited=true 表示文件已保存，附带新 hash；不要盲目重试正文编辑，先检查索引。已有私人目录不自动迁移重写，完整策略见[Memory 模块](docs/modules/memory.md)。

`memory_history` / `memory_restore`、`memory.restore` 和 `/memory restore` 已移除。旧 Snapshot 的 CURRENT、versions 保留但迁移完成后不再使用；迁移前建立唯一完整备份，逐字节导出旧 Core，断点可继续，遇到新文件人工更改则拒绝覆盖。见[迁移步骤与冲突处理](docs/memory-migration.md)。长期恢复依赖用户备份，V0.5 没有版本链。

Base System Prompt 由程序维护，Core 继续以 user 角色 JSON 数据注入；Deferred 工具返回也只是低信任 ToolMessage。记忆不能改变 Base、工具注册或权限。自定义仓库内目录仍须整目录忽略且无已跟踪文件；程序每次写前验证，不自动改 Git。不要在记忆中存放 API Key、Token 或密码。详细工具参数和实际调用路径见[MemoryFS 模块](docs/modules/memory.md)。

## 使用 Conversation Recall

正常对话不需要额外命令：先在一个会话讨论“先实现 Memory 是为了保存稳定偏好”，然后 `/new` 并问“我们之前为什么决定先实现 Memory？”。模型可调用 recall_search 搜索 Memory，再用 recall_get 展开来源，并引用 Conversation ID 回答；未找到证据时应明确说明。新会话不会默认加载旧会话，只有本次按需检索的结果进入当前对话。

搜索只涉及同一 Agent 的 completed / failed Run 的 user / assistant 非空 text，不索引 Provider 原始 JSON、reasoning、工具参数或 Tool Result。搜索和展开都会验证 Agent 归属，模型不能指定 agent_id。旧工具调用只是审计数据，永不自动续跑或重试。

三个字符及以上使用 FTS5 trigram 字面子串；一至两个字符使用有界 LIKE，只检索最近 2000 条符合条件的文本、每条前 65536 字符，并返回 scope_limited。query 最大 120 字符，limit 最大 5；展开 before / after 各最多 3，总数最多 7 条，总文本最多 8192 转义后 UTF-8 字节，单次数据 JSON 最多 12288 字节。结果带来源 ID、序号、时间、Run 状态和截断标记。全文索引的短词限制与选择理由见 [SQLite 官方文档](https://www.sqlite.org/fts5.html#the_trigram_tokenizer)。

启动通过 schema 1→2 迁移原子回填已有文本，新消息与索引通过触发器同事务写入。索引只是候选定位，结果正文回到原始 messages 读取。损坏时返回安全错误，不自动修复或重试；关闭 CLI 后可在本地 Python 中通过 `SQLiteStore.rebuild_recall_index()` 显式重建，完整示例见 [Recall 模块](docs/modules/recall.md)。重建不需要 API Key，不修改原始历史。

## 长会话 Context Management

V0.6 保留完整 SQLite 消息日志，但每轮正常请求只处理有界工作上下文：Base + 最新 Core + 当前 Conversation 的低信任 Summary + 尚未覆盖的完整 Run 窗口 + 当前 Run 全部消息。Summary 不写 Core/Deferred，也不成为新的原始聊天消息；Recall 继续查原始历史。

低于水位且一页容纳历史时，没有额外摘要调用。达到输入水位或超过分页资源上限时，ContextBuilder.prepare() 只压缩最近保护区之外、已经 completed/failed 的完整 Run。摘要通过现有 Provider、空工具列表、独立输入/输出和超时分批生成，逐批原子提交正文与历史边界。重启和 /resume 只读摘要及有界后续历史，不每次全量反序列化会话。

每个正常 Provider 请求前都检查预算，包含工具 Schema、Provider reasoning/wire JSON、当前工具结果和预留输出。保留区域不拆开调用/结果，当前 Run 绝不压缩；太大且不可缩减时返回 context_error，不静默丢弃。摘要失败返回 compaction_error，写库失败沿用 storage_error/interrupted；失败批次不推进边界，已经提交的前几批仍可恢复。不会自动重试或执行历史工具。

`/context status` 只显示安全统计，不显示 Summary、私人正文或 reasoning。estimated_tokens 是保守 UTF-8 序列化估算，Provider 返回的 input/output usage 另列；估算不是精确 tokenizer 或任意模型窗口的保证。摘要也是有损工作记录，重要内容可用 Recall 核实。

近期保留默认至少 2 个完整 Run，历史每页默认 32 Run、最多 2 MiB、单 Run 最多 2048 条。单个无法放进摘要输入的 Run 会明确拒绝，不在内部任意截断。所有参数和实际运行路径见[Context Budget 与 Compaction](docs/modules/compaction.md)。生产嵌入也应显式传 ContextBudget；无预算只兼容有资源上限的短嵌入模式，不提供模型窗口保证。

## 显式 Reflection 与长期知识整合

`/reflect` 只分析当前 Conversation 尚未审阅的 completed Run 原始历史，按完整 Run 和预算处理一批；不会把 Compaction Summary 当事实来源。先提取有证据的候选，再选择相关 Core/Deferred 记忆进行比较，生成新增、局部更新、no-op、拒绝或待判断计划。生成计划不修改 Markdown。

```text
/reflect
/reflect status
/reflect review
/reflect apply <plan_id> <item_id|all>
/reflect reject <plan_id> <item_id|all>
/reflect skip <plan_id> <item_id|all>
/reflect reconcile <plan_id> <item_id>
```

review 会展示候选价值、来源 Conversation/Run/sequence/引文、目标文件、局部 Before/After、预期 hash 和逐项状态。请核对事实、纠正顺序及 Diff 后，再带具体计划 ID 执行 apply；可逐项批准、拒绝或跳过。普通对话、历史和工具结果不能代替这个可信操作。待审阅计划跨重启保存；未决计划直接返回，不重复提取。空候选或所有条目已处理时，独立 Reflection 进度与关闭状态原子提交，下一次继续新历史。

Apply 复用 MemoryFS 的精确局部编辑、expected_hash、原子替换、路径/隐私检查和索引维护。编辑器改动会产生冲突；多项写入保留已成功项，不做整体回滚。partial/applying 表示文件可能已落盘，不能盲目重试：先 review，必要时显式 `/memory check`、`/memory repair`，再 reconcile 核对。reconcile 不写文件，仅核对目标 hash 和索引；skip/reject 不撤销已有副作用。

默认每次最多 8 个完整 Run、8 个候选、三次无工具 Provider 请求，输入/输出/超时独立配置为 16384/16384/60 秒，另有独立的 32768 字节可见 JSON 上限；见 `.env.example` 的 AGENT_REFLECTION_* 和[配置说明](docs/modules/config.md)。较大目录/正文或单 Run 无法容纳时明确停止。默认只反思当前会话；可 /resume 其他会话分别处理，同一 Agent 最终共享 Markdown 理解。模型事实判断与语义去重仍有误差，审批不可省略。设计与完整调用路径见 [Reflection 模块](docs/modules/reflection.md)。

## 架构与实际运行路径

详细设计见[整体架构与 Pipeline](docs/architecture.md)，包括模块依赖图、请求时序图、四种历史视图、事务边界、Run 状态转换及启动/切换/恢复流程。[开发文档索引](docs/README.md)提供阅读顺序。

```text
main → runtime.open_application → SQLiteStore.initialize(schema 16) / recover_incomplete_runs → SessionManager(Continuous Stream)
     → ContextBuilder(ConversationHistory) → Agent.run
          ↓ begin：保存 running Run + 用户消息
       ContextBuilder.prepare → Core/时间/有界历史/Working Summary + 可选 Thread/Episode/raw（不写 Journal）
       Provider.generate（内部消息 / 本轮授权的工具定义）
          ↓ record：先保存模型调用
       ToolRegistry.execute → 保存结果 → 下一轮模型请求
          ↓ commit：完成 Run 状态
       RunResult → CLI 先显示答案 → 可抢占的 MemoryAutomation / 空闲 Episode → 低频 Thread
```

- Agent 核心依赖历史 Protocol，不依赖 sqlite3、CLI 或 SDK 的具体响应对象。
- SQLite 保存 Agent / Conversation / Message / Run，client_state 记录上次选择。
- Message 保留完整 role、文本、工具参数、call_id、结果和 provider_data，不只保存展示文本。
- `user_version` 管理 schema；当前使用 schema 16；11 追加学习任务与水位，12 追加角色状态与来源，13 追加 QQ 收件回执，14 追加主 Agent 模型调用审计快照，15 追加图片正文与 QQ 附件字段，16 追加 Proactive Shadow 水位、候选和判断审计；旧迁移定义不改，原 Journal/Summary/Reflection 保留。迁移失败回滚，遇到更高版本或陌生数据库不覆盖。
- ContextBuilder 区分完整审计、有界可重放窗口、会话摘要和运行中消息；摘要不会删除原历史。
- Provider 继续使用 Responses API 的完整本地上下文、`store=False` 和 reasoning 项目重放，持久化前后使用相同映射。工具调用格式参考 [OpenAI 官方函数调用文档](https://developers.openai.com/api/docs/guides/function-calling)。

模块文档均包含架构组成、真实入口/执行顺序、异常边界和验证方法：

- [整体架构与端到端 Pipeline](docs/architecture.md)
- [V0.5 设计调研](docs/memory-design.md) 与 [迁移说明](docs/memory-migration.md)
- [Agent Loop](docs/modules/agent.md)
- [共用证据投影与校验](docs/modules/evidence.md)
- [ContextBuilder 与历史边界](docs/modules/context.md)
- [Episode、Thread 与有界事后整理](docs/modules/organization.md)
- [Dirty Thread 的当前来源重建与一致性](docs/modules/thread-repair.md)
- [Idle、周期检查与前台抢占](docs/modules/organization-schedule.md)
- [Working Context Retrieval 与来源去重](docs/modules/retrieval.md)
- [P1 迁移、使用、交付与 P2 接口](docs/p1-delivery.md)
- [P1 Review Fix：时间驱动与一致性报告](docs/p1-review-fix.md)
- [Context Budget、Compaction 与持久摘要](docs/modules/compaction.md)
- [Reflection、证据、审批与长期记忆整合](docs/modules/reflection.md)
- [回答后的 Memory Automation 与可信策略](docs/modules/automation.md)
- [隔离合成数据的自动化行为评测](docs/modules/evaluation.md)
- [模型轮次与 Prompt 审计](docs/modules/tracing.md)
- [内部及持久化数据模型](docs/modules/models.md)
- [Provider 与 Responses 重放](docs/modules/provider.md)
- [工具注册与超时契约](docs/modules/tools.md)
- [MemoryFS、分层索引及权限](docs/modules/memory.md)
- [Conversation Recall、中文检索及重建](docs/modules/recall.md)
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

测试使用 Fake Provider、SDK MockTransport、临时 SQLite 和记忆目录，隔离真实配置并禁用外网，不需要真实 API Key，不消耗模型费用。覆盖重启恢复、会话隔离、完整消息/Provider JSON 重放、迁移回滚、数据库异常、批量工具调用中断、真实进程退出和多次重启，以及原有 Runtime 测试。 Core 回归覆盖跨会话记忆、更新冲突、原子写失败、大小限制、权限过期和 Base System Prompt 隔离。 Recall 测试覆盖 旧库回填、索引事务回滚、字面检索、跨 Agent 隔离、展开限制、索引重建及恶意历史的权限边界。 V0.5 增加旧格式迁移中断、编辑器可见性、索引补链/部分失败、Deferred 分页、Junction 和 CLI 重启渐进读取测试。 Windows 沙箱临时目录受限时使用 `uv run pytest --basetemp=.pytest_cache/tmp`； asyncio 需要允许本地 socketpair，测试仍禁止外网。 uv 缓存目录不可写时设置 `$env:UV_CACHE_DIR = "$PWD/.uv-cache"`。

共享渠道装配与前台执行入口见 [Shared Chat Runtime](docs/modules/runtime.md)。

QQ 官方协议、Transport 与离线测试见 [QQ 渠道](docs/modules/qq.md)。

专用 QQ 小号的一对一聊天：`uv run personal-agent napcat --dashboard`，通过本机 NapCat OneBot 正向 WS 接入原 Runtime，只接受配置主号的好友私聊，支持文本和图片输入、文本回复。配置、登录及手工检查见 [NapCat 接入指南](docs/napcat-setup.md)，实际调用路径与离线测试见 [NapCat 模块](docs/modules/napcat.md)。不需要官方 AppID/OpenID；主动消息仍保持 Shadow。

QQ 连续消息按静默 2.5 秒、首条起最多 8 秒聚合为一次输入；原子领取、图片限制、重启语义与手工验证见 [QQ 消息聚合](docs/modules/qq-turns.md)。

QQ 默认将普通聊天自然拆成最多四个气泡，并使用计入生成耗时的轻量条间停顿；代码、列表与链接等结构化内容保留原字节分段策略。可通过 `AGENT_QQ_HUMANIZE_ENABLED=false` 关闭，配置与体验方法见 [聊天气泡与停顿](docs/qq-setup.md#聊天气泡与停顿)。完整回答仍保留在原 Journal，不增加模型调用。

P4-MVP 增加官方 QQ 单人 C2C 文本入口：`uv run personal-agent qq`。先用 `uv run personal-agent qq --discover-openid` 取得并人工核对测试账号的 OpenID，再配置单用户白名单。诊断不调用模型；普通 QQ 对话复用原有会话、Memory 和 Character。完整平台准备、配置、Smoke Test 与限制见 [QQ 接入指南](docs/qq-setup.md)。CLI 与 QQ 不能以两个进程同时使用相同数据库。

## 当前限制

V0.1 原有的进程内历史无法事后导入，V0.2 从首次启动时开始保存。 SQLite 操作同步执行，使用短事务和 2 秒繁忙等待，适合单人本机，不支持并发多进程服务。正常路径只加载有限工作页和摘要；没有精确 tokenizer、向量检索、自动备份或数据加密。摘要有损、估算不精确，单个不可缩减的大 Run 需调整预算或新建会话。记忆工具具备按 Run 授权，通用外部工具的细粒度权限、幂等和任务恢复仍需后续设计。记忆读取、Git 检查和提交均在事件循环中同步执行，async def 包装不意味着可及时取消；磁盘卡顿不能被 asyncio 超时中断；不保留日常版本历史。 Markdown 单文件发布与 SQLite 结果保存没有共同事务：后者失败不能撤销已提交的记忆。原子文件替换不等于断电级持久性，当前无目录 fsync 或网络文件系统保证。路径与摘要校验不是 OS 沙箱；低优先级数据不代表模型绝不受恶意记忆文本影响。

Recall 采用关键词子串与时间排序，尚无语义召回或分页；短查询和长消息正文会受范围/长度上限限制。SQLite 读取、回填与重建是同步操作，查询工作预算不是硬实时取消。索引错误可能使新消息事务失败，需要显式修复后继续；外部 VACUUM 等维护改变内部 rowid 后也应重建索引。

外部编辑器不参与受控写锁，最后摘要检查与 replace 仍有竞争窗口；多文件索引无整体事务，需显式检查/修复。索引只支持约定的根相对 Markdown bullet 链接和简化 Frontmatter，不提供任意 YAML、无限目录或自动语义搜索。迁移保留的旧目录及备份由用户手动清理。

V0.6 新增离线 Compaction 验证：schema 2→3 回填与回滚、增量检查点、完整工具与 SDK 重放、分批失败续接、取消和真实进程退出、跨会话隔离、SQL 范围与解码数量、输出/超时限额及注入权限边界。原完整 Journal 与 Recall API 继续保留。

V0.7 的离线 Reflection 验证覆盖来源校验、整合计划、选择性审批、拒绝/跳过、增量分页、schema 3→4 原子迁移、人工编辑冲突、索引部分成功和真实 CLI 子进程崩溃/重启。完整回归 390 passed、1 skipped（Windows Symlink 权限）；Ruff、打包与 CLI 检查见交付记录。

V0.7.1 分离 Reflection API 总输出预算（含 reasoning，默认 16384 tokens）与可见 JSON 字节上限（默认 32768），避免把 tokens 当作 UTF-8 字节。非 completed 响应返回安全的阶段、停止原因和用量计数，不打印推理或未完成正文；不自动重试。若本地显式配置过旧 AGENT_REFLECTION_OUTPUT_TOKENS=4096，需要自行调整才会覆盖旧值。

使用 deepseek-flash 时，可将 AGENT_CONTEXT_WINDOW 明确设为 1048576；这是目前[官方模型元数据](https://api-docs.deepseek.com/api/list-models/)给出的总输入/输出窗口。程序仍要求宿主显式指定，不按模型名静默猜测。

诊断、预算修复与真实提案质量对照见 [V0.7.1 真实模型验证](docs/reflection-live-validation.md)。

## V0.7.2 Memory Automation（Phase 1，兼容模式）

P2 Learning 默认接管自动入口；本节模式仅在 AGENT_MEMORY_LEARNING_ENABLED=false 时运行，旧计划和 /reflect 保留。

关闭 P2 Learning 后，普通 Run 成功先展示回答，再按兼容 Automation 模式进行 LLM Worthiness 检查，有价值才复用 Reflection 生成原有可审阅计划。P1 在本地可抢占的异步钩子内执行，下一次输入取消并等待记账结束。该兼容模式默认 suggest，未批准不写 Markdown；调用/预算/等待有界，失败不改变已完成的回答，也不自动重试。已有计划时提示积压，新历史不会被覆盖或误标写入。

```text
/memory automation status
/memory automation off
/memory automation suggest
/memory automation run
/reflect review
/reflect apply <plan_id> <item_id|all>
/memory automation retry
```

AGENT_MEMORY_AUTOMATION 可配置 off/suggest/auto。auto 只接受事前审核的 knowledge/ 新文件精确路径与 Markdown SHA256 授权，限小文件，不自动修改 Core 或旧知识；未匹配则保留审阅，不能由模型输出开启。这个保守模式不是通用自主写入，详情与全部配置见[Memory Automation](docs/modules/automation.md)。


## V0.7.2 Phase 2：评测与 Prompt 选择

基线冻结在 Phase 1 完成后的 `7ac3b6e`。16 个固定合成 Case 覆盖 12 类行为，12 开发、4 Holdout，各重复两次；分组测试后保留 Main/Tool Descriptions/Worthiness，Reflection/Compaction 候选没有证明语义质量收益而撤回。检索开发组 11/14 → 14/14，最终组合 Holdout 7/8 → 7/8，仍有 human.md 被误选 create 后由 Host 拒绝的失败，不能宣称整体质量提升。

真实评测与 pytest 分开，必须显式 `--live` 才产生费用，不使用私人会话，不执行 Apply。此次 64 次场景运行、283 次 API 请求，按峰值 cache-miss 单价估算 $0.262，并非实际账单。普通请求、自动记忆与 Compaction 分开计量。原始合成数据留在忽略目录，只提交无正文汇总、固定语料和 Prompt 快照。

- [Prompt Audit 与修改假设](docs/prompt-audit.md)
- [A/B 结果、退化、用量与限制](docs/prompt-ab-results.md)
- [执行路径与复现命令](docs/modules/evaluation.md)
- [CLI Dogfooding 与审阅检查](docs/cli-dogfooding.md)

```powershell
uv run python -m personal_agent.evaluation --list-cases
uv run python -m personal_agent.evaluation --help
# 不调用模型，保存当前实际 Prompt；已有快照拒绝覆盖
uv run python -m personal_agent.evaluation --snapshot .heybuddy/current-prompts.json
```

## HeyBuddy Observatory

本地只读观测面板：`uv run personal-agent qq --dashboard` 将 QQ 与 Dashboard 放在同一个 Shared ChatService 进程；`uv run personal-agent web` 独立查看持久历史（须先退出已有实例）。默认地址为 [http://127.0.0.1:8765](http://127.0.0.1:8765)，可用 `--dashboard-port 8766` 更换端口。固定监听本机，不支持公网绑定。

模块说明见 [Observatory](docs/modules/dashboard.md)，启动、截图、数据源与验收记录见 [交付与验收](docs/observatory.md)。

[通用交谈提示词](src/personal_agent/prompts/conversation.md) 独立于角色卡，统一日常闲聊的默认表达；装配路径、发送配合与边界见 [模块说明](docs/modules/prompts.md)。

QQ 现支持图片、图文和 JPEG/PNG/GIF/WebP 表情包输入；动图只读取首帧，回复仍是文本。下载有官方 CDN、大小、像素和总超时限制，使用现有 Agent/角色/记忆与 SQLite 附件存储。接口、上下文预算、失败和验证见[图片输入](docs/modules/images.md)。
