# 配置与 API 地址校验

## 职责与架构组成

`src/personal_agent/config.py::Settings` 使用 Pydantic 校验密钥、模型名、Base URL、轮数、超时、日志级别、数据库路径与记忆大小/根目录；密钥以 `SecretStr` 保存。 `ENV_FIELDS` 映射环境变量到内部字段，`load_settings()` 只读取指定文件或当前目录的 `.env`。 `main.py::main()` 加载配置，`run_cli()` 根据配置装配 SDK 客户端和 Runtime。这里不执行 API 请求，不解析 SDK 响应，也不搜索父目录凭证。

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

最小用法：`Settings(api_key="test-key", model="test-model", base_url="http://127.0.0.1:8000/v1")` 通过；将地址换成 `http://api.example.com/v1` 则触发 `ValidationError`。通过环境变量设置 Base URL 也经过相同校验。

对应 `tests/test_config_cli.py`：

- `test_base_url_allows_https_and_explicit_loopback_http`
- `test_base_url_rejects_non_loopback_http_and_hostname_spoofs`
- `test_environment_base_url_cannot_bypass_https_policy`

运行：`uv run pytest tests/test_config_cli.py`。测试隔离环境和临时配置，不读取真实 `.env`。


## V0.4 Recall 的运行要求

未新增环境变量或依赖。Python 自带 SQLite 必须支持 FTS5 trigram（本地验证为 3.47.1）；schema 2 初始化回填失败会以原有存储启动错误退出，不降低成无界扫描。搜索和输出上限是 Recall 模块中的保守常量；短词范围限制始终在工具描述及结果中说明。只做索引重建的宿主 API 不需要配置模型或 API Key。
