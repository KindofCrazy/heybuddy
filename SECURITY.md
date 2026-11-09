# 安全说明

## 报告安全问题

涉及凭据泄露、越权工具执行、路径逃逸或私人会话暴露的问题，请通过仓库 [Security 页面](https://github.com/KindofCrazy/heybuddy/security)中的 **Report a vulnerability** 私下报告。若该入口不可用，可在公开 Issue 中仅请求私下联系渠道，不附漏洞细节、利用方法或敏感数据。

报告请尽量提供受影响提交、环境、预期边界、使用合成数据的最小复现，以及影响范围。不要附真实 `.env`、API Key、聊天记录、数据库或 Memory。若密钥已经泄露，应先在对应服务撤销或轮换，再提供脱敏信息。

当前维护重点是 `main` 分支。项目尚未承诺旧版本安全补丁周期或固定响应时限。

## 当前安全边界

- SQLite 和 Markdown Memory 均为本地明文，没有内置加密或日常自动备份；部署者负责目录访问控制和备份。
- `.env`、运行数据与记忆不得提交到 Git。MemoryFS 写入会检查实际 Agent 目录的 Git 隐私状态；自定义数据目录建议放在源码仓库外。
- 工具由可信 Host 注册并授权，模型文本、历史和记忆不能增加权限；不开放任意 Shell、模型生成代码执行或配置管理。
- Skills、MCP、外部内容和历史证据不能作为新的宿主权限来源。配置 MCP 或渠道前应核对允许的服务、工具与账号范围。
- Observatory 仅用于 loopback 只读观察，不应直接暴露到公网。QQ / NapCat 保持文档规定的白名单、账号绑定和操作范围。
- 中断恢复不自动重跑未知副作用。手动 Reflection 审批和可信 Workflow Resume 不能从普通聊天或历史消息触发。

这些约束用于限制应用行为，不构成针对本机恶意用户的操作系统级沙箱。实现与操作说明见 [AGENTS.md](AGENTS.md)、[配置](docs/modules/config.md)、[工具](docs/modules/tools.md)、[Memory](docs/modules/memory.md)和 [Observatory](docs/observatory.md)。
