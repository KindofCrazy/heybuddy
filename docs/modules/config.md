# 配置与 API 地址校验

当前开发示例显式配置 AGENT_CHARACTER_ID=luna，读取内置 OpenPersona JSON 角色；Settings 的库级默认仍是 none。程序不自动覆盖已有显式配置。角色修改在下次装配/重启时生效，不修改数据库、历史或记忆路径。完整说明见 [简化角色设定](persona.md)。

P2 Review Fix 的 AGENT_MEMORY_LEARNING_RECOVERY_ATTEMPTS/INTERVAL_SECONDS/SECONDS 默认 3/300/2，映射到 LearningPolicy 的有界检查次数、退避和协作时间预算。核对不调用 LLM，重新规划使用原维护额度；没有文件系统硬超时保证。详见 [Learning](learning.md)。

P3 `Settings.character_policy()/character_state_policy()` 对应 AGENT_CHARACTER_*。ID 默认 none，合法 ID 为小写字母起始、后接字母/数字/下划线，最大 48 字符；ROOT 仅可信本地目录。JSON 角色只使用文件与上下文字节预算，不使用示例/Lore 选择、E0/E1/E2 差异或 State。旧 Markdown 角色的 VARIANT 只接受 E0/E1/E2，State 仅在 E2 时启用且可独立关闭；Mood/Focus 有效期、维护间隔、输入输出/超时/来源都有界。完整默认值在 .env.example；实际接线见 [Persona](persona.md)、[Character](character.md)、[State](character-state.md)。

P2 的 `Settings.learning_policy()/curator_policy()/memory_retrieval_policy()` 对应 `.env.example` 的 AGENT_MEMORY_LEARNING_*、PRIORITY_ENABLED、CURATOR_*、RETRIEVAL_*，全部有界。默认启用 Learning、Curator 和条件性 Preflight；普通 Batch 3 Episodes/21600 秒，Curator 86400 秒/累计 12 更新或文件长度条件。P2 与 Episode/Thread 共用维护轮额度，不与旧 MemoryAutomation 同时自动运行。默认两目标学习在开始提取前保守预留四次请求容量；自定义预算不足显示 learning_budget_config，不付费重复提取。输入/输出/来源字节/文件数及各阶段超时见 [Learning](learning.md)、[Curator](curator.md)、[Retrieval](retrieval.md)。关闭 P2 恢复旧 Automation 配置，关闭 Memory Retrieval 不清空既有 Markdown。配置来自可信 Host，模型不能修改开关或权限。

Review Fix 增加 AGENT_ORGANIZATION_THREAD_INTERVAL_SECONDS（14400）、CHECK_SECONDS（60）、COOLDOWN_SECONDS（60），配合已有 1500 秒 Idle、8 Runs、3 Episodes 和 10800 秒 Finalize 窗口。触发与时间源语义见 [Organization Schedule](organization-schedule.md)。

MAINTENANCE_BATCHES/REQUESTS/TOKENS/SECONDS 默认 4/4/131072/120，是一轮共享维护上限；REPAIR_RETRY_SECONDS/MAX_FAILURES 默认 300/3，控制同源 Repair 的退避和自动失败上限。MemoryAutomation 保持自己的额度，未混入维护轮计量。

P1 的 Settings.organization_policy() 构造可信 OrganizationPolicy，所有 `organization_` 字段按 `AGENT_ORGANIZATION_` 环境变量映射。校验数量、超时、字节/输出预算和递增的 weak/strong/split 时间阈值；模型不能修改配置。完整参数、运行链路与离线测试见 [Organization](organization.md)。

Settings.retrieval_policy() 对应 `AGENT_RETRIEVAL_EPISODE_LIMIT`（1–3，默认 3）、RAW_RUN_LIMIT（1–3，默认 2）、MAX_RUN_BYTES（默认 65536）、OPTIONAL_TOKENS（默认 12288）及 SCAN_EPISODES（默认 200）。ContextBuilder 使用同一可选 tokens 上限并进一步按总窗口和触发水位缩减；检索器只负责相关性，不能扩大预算。Organization 的 SOURCE_BYTES/RESPONSE_BYTES 分别限制原始来源和可见输出，默认 65536/32768，与 tokens 独立。配置测试见 tests/test_config_cli.py。

## 职责与架构组成

`src/personal_agent/config.py::Settings` 继承 ContextBudget，使用 Pydantic 校验密钥、模型名、Base URL、轮数、超时、日志级别、数据库路径与记忆大小/根目录；密钥以 `SecretStr` 保存。 `ENV_FIELDS` 映射环境变量到内部字段，`load_settings()` 只读取指定文件或当前目录的 `.env`。 `main.py::main()` 加载配置，`run_cli()` 根据配置装配 SDK 客户端和 Runtime。这里不执行 API 请求，不解析 SDK 响应，也不搜索父目录凭证。

## 代码实际运行路径

1. `main()` 调用 `load_settings()`。
2. `load_settings()` 用 `dotenv_values()` 解析当前目录的 `.env`，或调用方明确指定的文件；禁用变量插值，兼容 UTF-8 BOM。
3. 对 `ENV_FIELDS` 中每一项，先取系统环境变量，再取文件值，构造待校验数据。
4. `Settings.model_validate()` 校验字段：模型去空白、拒绝空密钥、检查正轮数/超时和日志级别。
5. Base URL 首先经 `HttpUrl` 解析，随后 `require_secure_base_url()` 执行安全策略：未设置或使用 HTTPS 时通过；HTTP 仅允许解析后的主机名为 `localhost`（可有结尾的点），或 `ipaddress.ip_address(host).is_loopback` 为真的数值地址。 IPv6 主机名先移除方括号，不执行 DNS 查询。
6. `AGENT_DATABASE_PATH` 默认 `.heybuddy/heybuddy.sqlite3`，相对启动目录解析。 `run_cli()` 先打开数据库、初始化 schema、处理遗留 running Run 并恢复会话，再装配 SDK。未设置 Base URL 时使用 SDK 默认 OpenAI 地址。校验失败时 CLI 仅报告出错字段并以退出码 2 结束，不输出原始输入值或密钥。数据库无法打开、被其他实例占用或初始化失败时返回退出码 1，不悄悄创建临时会话替代。

## 记忆配置与启动

AGENT_MEMORY_ROOT 默认为空。`memory_root_for(settings, agent_id)` 先使用显式根；否则沿用已存在的 `<database_path>.memory/<agent_id>`，避免升级后丢失原位置；新安装用 `default_memory_root()` 的用户数据目录（Windows LOCALAPPDATA/HeyBuddy/memory，其他系统 ~/.local/share/heybuddy/memory），再按 Agent ID 隔离。程序只检查这一个已知旧位置，不搜索其他路径。根/父目录的 Symlink/Junction 在 MemoryFS 内拒绝。

AGENT_CORE_MEMORY_MAX_BYTES 默认 8192，范围 512～1048576，限制三块编译 JSON 的 UTF-8 总字节；不含 Deferred 正文，超限不截断。MemoryFS 初始化会执行一次性旧格式迁移，校验或隐私失败返回退出码 1，参数越界为配置错误退出码 2。仓库内实际 Agent 目录须整目录被忽略且无已跟踪文件，检查依赖可用 Git，失败关闭；不改 .gitignore 或索引。具体步骤见[迁移说明](../memory-migration.md)。

## 异常与边界

允许示例：`https://api.example.com/v1`、`http://localhost:8000/v1`、 `http://127.0.0.1:8000/v1`、`http://[::1]:8000/v1`。 HTTP 的数值 loopback 范围为 IPv4 `127.0.0.0/8` 和 IPv6 `::1`。拒绝公网 HTTP、局域网 HTTP、未指定绑定地址 `0.0.0.0` / `::`，以及 `localhost.example.com`。使用 URL 解析后的主机做判断，路径或 userinfo 中包含 `localhost` 不会放行远端主机。

HTTPS 校验只保证配置的初始地址符合传输策略，不验证模型能力、服务身份、可用性或重定向目标。 loopback 服务及本机 DNS/hosts 配置仍需用户信任；当前不实现网络隔离或端点授权系统。

## 阅读与验证

最小用法：`Settings(api_key="test-key", model="test-model", context_window=64000, base_url="http://127.0.0.1:8000/v1")` 通过；将地址换成 `http://api.example.com/v1` 则触发 `ValidationError`。通过环境变量设置 Base URL 也经过相同校验。

对应 `tests/test_config_cli.py`：

- `test_base_url_allows_https_and_explicit_loopback_http`
- `test_base_url_rejects_non_loopback_http_and_hostname_spoofs`
- `test_environment_base_url_cannot_bypass_https_policy`

运行：`uv run pytest tests/test_config_cli.py`。测试隔离环境和临时配置，不读取真实 `.env`。


## V0.4 Recall 的运行要求

未新增环境变量或依赖。Python 自带 SQLite 必须支持 FTS5 trigram（本地验证为 3.47.1）；schema 2 初始化回填失败会以原有存储启动错误退出，不降低成无界扫描。搜索和输出上限是 Recall 模块中的保守常量；短词范围限制始终在工具描述及结果中说明。只做索引重建的宿主 API 不需要配置模型或 API Key。

## V0.6 Context 配置

AGENT_CONTEXT_WINDOW 必填，单位为模型窗口 tokens；不按模型名或自定义端点猜测数值。其他配置继承 ContextBudget 的保守默认值，Settings.context_budget() 只将预算字段传给 SessionManager/ContextBuilder。组合约束检查输出/余量后剩余空间、摘要容量，以及 page_runs 大于 recent_runs，失败为安全配置错误退出码 2，根级错误显示 context_budget，不输出 Pydantic 输入值。

| 环境变量 | 字段 | 默认 |
| --- | --- | --- |
| AGENT_CONTEXT_WINDOW | context_window | 必填，4096～4000000 |
| AGENT_RESERVED_OUTPUT_TOKENS | reserved_output_tokens | 4096 |
| AGENT_CONTEXT_SAFETY_MARGIN | safety_margin | 1024 |
| AGENT_COMPACTION_TRIGGER_RATIO | trigger_ratio | 0.8 |
| AGENT_RECENT_RUNS | recent_runs | 2，至少保护的完整 Run 数 |
| AGENT_HISTORY_PAGE_RUNS | page_runs | 32，2～64，须大于 recent_runs |
| AGENT_SUMMARY_TOKENS | summary_tokens | 2048，摘要正文的本地估算上限 |
| AGENT_SUMMARY_OUTPUT_TOKENS | summary_output_tokens | 4096，摘要调用输出限额 |
| AGENT_COMPACTION_INPUT_TOKENS | compaction_input_tokens | 16384，另受模型窗口/输出/余量限制 |
| AGENT_COMPACTION_TIMEOUT_SECONDS | compaction_timeout | 30，独立摘要请求超时 |
| AGENT_MAX_COMPACTION_STEPS | max_compaction_steps | 16，每次准备最多批数 |

输出限额需匹配具体模型能力；reasoning 也消耗输出预算。较小窗口通常还需调低输出/摘要上限，不能只修改窗口使默认组合不一致。估算按 UTF-8 序列化字节保守计量，非精确 tokenizer，不同端点的真实上下文/最大输出应由用户查证。请求预算范围和不可缩减行为见[Compaction](compaction.md)。所有新测试继续隔离真实配置，不访问真实 API。

## V0.7 手动 Reflection 配置

Settings.reflection_policy() 只将以下参数传入 ReflectionPolicy，与普通请求/Compaction 独立。无新依赖或第二个模型配置，复用同一 Provider。所有值均可通过 `.env` 或环境变量设置，环境优先；测试环境仍按 ENV_FIELDS 隔离，未改网络禁用保护。

| 环境变量 | 默认 | 边界 |
| --- | --- | --- |
| AGENT_REFLECTION_INPUT_TOKENS | 16384 | 2048～262144；实际还减去模型输出和安全余量 |
| AGENT_REFLECTION_OUTPUT_TOKENS | 16384 | 256～65536；API 总输出 tokens，含推理及可见正文 |
| AGENT_REFLECTION_RESPONSE_MAX_BYTES | 32768 | 1024～131072；可见 JSON UTF-8 字节上限，与 tokens 独立 |
| AGENT_REFLECTION_TIMEOUT_SECONDS | 60 | 每阶段独立有限正超时 |
| AGENT_REFLECTION_PAGE_RUNS | 8 | 1～32 个完整 completed Run，每次手动处理一批 |

Reflection 输入不足时只在触发 /reflect 时明确失败，不因未使用的 Reflection 预算阻止普通聊天或只读 review/status。每阶段包含 Schema/JSON 封装后重新检查；无重试或静默截断。候选/事实判断必须经用户审阅，见[Reflection](reflection.md)。

V0.7.1 修正旧版将 output_tokens 同时当 JSON 字节上限的混用。输入上限仍减去完整 API 总输出额度与 safety_margin，不能为增加输出忽略模型窗口；可见 JSON 另外限长，再进入 Pydantic、来源和文件校验。默认额度提高是为推理预留空间，不能保证任意响应必然完成，也不会无限增加额度或重试。

目前 [DeepSeek 模型元数据](https://api-docs.deepseek.com/api/list-models/)声明 deepseek-flash 总窗口为 1048576 tokens；本地可显式配置 AGENT_CONTEXT_WINDOW=1048576。未知模型仍必须由用户指定；最大窗口不会自动全量加载历史或 Deferred Memory。已有配置值优先于新默认值。


## V0.7.2 Phase 1 集成

V0.7.2 新增 AGENT_MEMORY_AUTOMATION=suggest、AGENT_MEMORY_AUTOMATION_MAX_REQUESTS=4、AGENT_MEMORY_AUTOMATION_MAX_TOKENS=131072、AGENT_MEMORY_AUTOMATION_TIMEOUT_SECONDS=180。AGENT_MEMORY_AUTO_CREATE_GRANTS 为精确 knowledge/ 路径到 Markdown SHA256 的 JSON 对象，默认空；Settings.automation_policy() 校验模式、路径/摘要/预算，不从记忆和聊天获得授权。
# QQ 可选配置

`Settings` 同时继承 `channels.napcat.config.NapCatSettings`；`AGENT_NAPCAT_ENABLED/WS_URL/TOKEN/SELF_ID/PEER_ID` 通过 `ENV_FIELDS` 加载。NapCat 入口先检查启用、两个不同 QQ 号码和 Header 安全 Token，不要求官方 QQ 凭据。URL 仅接受带显式非特权端口的 literal loopback WS，拒绝域名、query 和 URL 内凭据。收件、图片、聚合及发送限额复用 `AGENT_QQ_*`。见 [NapCat](napcat.md) 与[配置步骤](../napcat-setup.md)。

`Settings` 同时包含 `channels.qq.config.QQSettings`，`ENV_FIELDS` 将 `qq_` 字段映射为 `AGENT_QQ_*`。只有 QQ 启动入口调用 `require_qq()` 检查 enabled、凭据与单一 OpenID，原 CLI 不要求 QQ 凭证。SecretStr 不显示明文；白名单拒绝通配符与多用户。队列、超时、回复窗口/段数、UTF-8 字节、重连次数和退避都设上限，详见 [qq.md](qq.md)。

自然拆条与轻量停顿默认开启，由 `AGENT_QQ_HUMANIZE_ENABLED` 统一关闭。`MS_PER_CHAR=25`、`MAX_DELAY_SECONDS=1.5`、`GAP_SECONDS=1.2`、`JITTER=0.2` 均带 `AGENT_QQ_HUMANIZE_` 前缀；分别限制在 0–150、0–5、0–2、0–0.5，拒绝非有限值。设暂停上限为 0 仅保留拆条。字段通过同一映射进入完整 `load_settings()` 和只需 QQ 配置的 `load_qq_settings()`，不读取角色或更改模型请求。验证见 `tests/test_qq_humanize.py`。

## 宿主工具数量限制

AGENT_MAX_TOOL_CALLS_PER_RESPONSE 默认 64、范围 1–256；AGENT_MAX_TOOL_CALLS_PER_RUN 默认 256、范围 1–1024。Settings 将它们传给原 Agent；在响应进入工具调用 Journal 或执行前检查整批数量，超限返回 invalid_response，已执行的更早批次保留，不截取执行部分批次、不自动重试。限制与 max_rounds/模型输出 token 限额独立。测试见 tests/test_agent.py；原 write-before-execute 和结果配对校验保持。

## 显式 1M 整理配置

整理输出允许 256～1,000,000 tokens，默认仍为 8192；该额度包含推理。需要大输出时，可显式设置 `AGENT_CONTEXT_WINDOW=1048576`、`AGENT_ORGANIZATION_OUTPUT_TOKENS=1000000`、`AGENT_ORGANIZATION_MAINTENANCE_TOKENS=1048576`。窗口需容纳输入、输出和安全余量；输入上限仍为默认 24576，单轮维护按实际输入估算加完整输出额度预留，因此该配置通常一轮只能容纳一次整理调用。API 是否接受这些值取决于实际模型和端点，宿主校验通过不表示服务端支持。

`load_settings()` 在下次启动时读取配置，环境变量优先于本地文件。超时、可见 JSON 字节上限和失败显式重试规则保持独立；增大 tokens 不会自动恢复 failed 批次。离线端到端验证见 `tests/test_episodes.py::test_million_token_configuration_reaches_single_bounded_request`。

例如 [DeepSeek Chat API 文档](https://api-docs.deepseek.com/api/create-chat-completion/)目前规定 deepseek-flash 最大输出为 393216 tokens，1M 上下文不等于 1M 输出；使用此模型时应将上述 OUTPUT_TOKENS 改为 393216。宿主保留 1,000,000 的可配置上界，端点能力仍需分别核对。
# Proactive Shadow 配置

`Settings.proactive_policy()` 将 `AGENT_PROACTIVE_*` 可信配置转为 `ProactivePolicy`。默认开启 Shadow，没有主动发送模式；字段通过 `ENV_FIELDS` 加载，仍不搜索其他凭证路径。预算、安静窗口、时区偏移、候选上限及执行路径见 [proactive.md](proactive.md)。

# Composer 预览开关

`AGENT_PROACTIVE_COMPOSE_ENABLED` 默认 true，由 Settings.proactive_policy() 传入 ProactivePolicy.compose_enabled，控制已提交 notify 后的草稿生成。它不授予投递权限，当前没有真实 Sender 配置；`AGENT_PROACTIVE_ENABLED=false` 仍关闭整个流水线。离线模拟只能通过可信 CLI 显式命令或 Harness 许可，详见 [outbox.md](outbox.md)。

## 策略定义依赖

`CuratorPolicy` 定义在 `learning/models.py`，Settings 仅从模型模块导入，不再为配置校验加载 Curator/Reflection 服务实现；curator 模块保留该导入供已有调用兼容。环境变量、默认值和验证约束不变。验证：`tests/test_config_cli.py`、`tests/test_curator.py`。
