# MCP：连接、工具发现与调用

技能的完整加载过程已独立为 [Skills](skills.md)。本文重点说明外部工具协议接入，仍保留与装配有关的技能目录步骤。

## 职责与架构组成

`skills/loader.py::SkillLoader` 读取 Host 安装的操作方法。`tools/mcp.py::MCPManager` 使用官方 MCP Python SDK 2 的真实 Client，统一发现、校验和注册外部工具。Skill、Memory、Persona 分别表达操作知识、用户事实与角色身份。

## 机制流程

1. 宿主预先决定允许连接哪些外部服务，以及每个服务的哪些工具可用。
2. 启动时建立连接，读取工具清单与参数规则，只把白名单内的能力接入本地工具体系。
3. 模型提出调用后，先通过与本地工具相同的校验、授权和预算检查，再发送给外部服务。
4. 把外部响应转为统一工具结果；连接失败标记服务不可用，已经发出的调用不自动重试。退出时关闭连接。

## 代码实际运行路径

1. HarnessRuntime 扫描 `AGENT_SKILLS_ROOT` 或用户数据目录 `HeyBuddy/skills` 下的 `*/SKILL.md`。YAML safe loader 读取 name/description，拒绝别名、重复名称、越界和过大文件。
2. 目录摘要经 ContextContributor 装配；skill_load 才读取完整正文并返回 hash。skill_read_resource 只读取同技能目录的有界文本资源，不运行其中脚本。
3. Host 可用 `AGENT_MCP_CONFIG` 指向本地 JSON 数组，每项含 name、command/args 或 url、allowed_tools、env_names。env_names 仅从进程环境读取；配置不存明文凭据。模型无连接配置接口。
4. open_chat 调用 MCPManager.start，10 秒内建立 stdio 或 Streamable HTTP 会话并分页发现工具；最多十页、100 个工具，只注册 allowed_tools。
5. 工具名由 Server/工具命名空间和稳定摘要组成，限制在 Registry 允许的字符及长度内。原 input_schema 交给 SchemaArguments，禁止外部 `$ref`，不生成有损参数模型。
6. 模型调用仍通过 Registry 的身份、参数、预算、Hook 和超时路径。MCP is_error 变为工具错误，文本和 structured_content 进入统一结果；大型结果交给 ReplayView 存档。未知二进制和资源 URI 不自动下载。
7. 单 Server 连接失败标 unavailable；已发出的工具请求不自动重试。应用退出按逆序关闭 Client 与 transport。

## 异常与边界

默认拒绝未配置工具，Server annotations 不授予权限。不配置 sampling、elicitation 或自动连接管理。HTTP 端点只来自 Host，远程必须 HTTPS，不继承环境代理；SDK 禁止跨来源重定向。stdio 使用显式环境映射，不继承模型 Key。Host 应仅将审核过的操作列入 allowed_tools。

技能最多 100 项，单文件 32 KiB，说明最多 500 字符；目录超出 ContextContributor 上限时明确报错。技能正文不改变系统规则、Memory 写权限或角色包。

## 阅读与验证

MCP 配置示例：`[{"name":"docs","command":"已审核可执行文件绝对路径","args":[],"allowed_tools":["search"],"env_names":[]}]`。真实配置放源码外或被忽略的本地目录。

`tests/test_harness.py` 启动临时 stdio Server 和 loopback HTTP Server 验证真实发现/调用、Schema 拒绝、工具许可与关闭；不会访问远程服务或真实模型。

## 未来改进

持久展示每个 Server 的工具版本与 Schema hash，发现配置漂移时要求宿主核对。区分连接失败、调用未发出、完成未知三种状态，避免把可重连理解为可重试副作用。技能加载路径单独见 [Skills](skills.md)。
