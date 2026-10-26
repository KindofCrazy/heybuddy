# 配置与 API 地址校验

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
| AGENT_REFLECTION_OUTPUT_TOKENS | 4096 | 256～16384；同时作为本地返回 JSON UTF-8 字节上限 |
| AGENT_REFLECTION_TIMEOUT_SECONDS | 60 | 每阶段独立有限正超时 |
| AGENT_REFLECTION_PAGE_RUNS | 8 | 1～32 个完整 completed Run，每次手动处理一批 |

Reflection 输入不足时只在触发 /reflect 时明确失败，不因未使用的 Reflection 预算阻止普通聊天或只读 review/status。每阶段包含 Schema/JSON 封装后重新检查；无重试或静默截断。候选/事实判断必须经用户审阅，见[Reflection](reflection.md)。
