# 参与贡献

欢迎通过 [Issue](https://github.com/KindofCrazy/heybuddy/issues)报告问题、讨论设计，或提交 Pull Request。项目持续开发中，涉及架构、权限、持久化格式或新渠道的改动，建议先说明使用场景和预期行为。

## 开发环境

需要 Git、uv 和 Python 3.13。安装步骤见[入门指南](docs/getting-started.md)。在仓库根目录执行：

```powershell
uv python install 3.13
uv sync --locked
uv run personal-agent --help
```

离线测试和 `--help` 不需要真实 API Key 或 `.env`。普通交互需要模型配置，不能用真实 API 调用代替自动化测试。

## 修改约定

- 保持改动聚焦，遵循现有类型标注、asyncio 和 Ruff 风格；不顺带重构无关模块。
- 修复问题时提供能复现行为的离线测试；新功能覆盖主要成功和失败路径。不要削弱 `tests/conftest.py` 中的网络与凭据隔离。
- 修改核心模块时，同步更新 `docs/modules/` 对应说明，描述实际入口、调用路径、异常与验证方式。新增模块在[文档索引](docs/README.md)中提供入口。
- 修改依赖时同步 `uv.lock`；SQLite 变更使用原子顺序迁移，保留完整原始 Journal。
- 不开放模型任意 Shell 或代码执行，不从用户文本、记忆或历史消息推导工具权限。Markdown 长期记忆与 SQLite 原始证据的职责保持分离。
- 不提交 `.env`、密钥、真实聊天、数据库、Memory 或未脱敏日志。示例和测试使用合成数据，第三方材料保留来源与许可。

完整项目约束见 [AGENTS.md](AGENTS.md)，尤其是数据一致性、MemoryFS、来源隔离和付费调用边界。

## 提交前验证

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

涉及打包、入口或 CI 时另执行：

```powershell
uv build
uv run personal-agent --help
```

GitHub Actions 在 Ubuntu 和 Windows、Python 3.13 上执行相同检查，并设置 `PYTHONUTF8=1`，让中文子进程输出不依赖 runner 的系统代码页。依赖安装可以联网，测试本身不得访问外部网络或真实账号。真实模型评测必须另行明确授权，通过隔离的显式 `--live` 入口执行。

## Issue 与 Pull Request

Bug 报告请提供提交版本、系统/Python/uv 版本、最小复现、预期与实际结果，以及脱敏后的安全诊断。功能建议请说明具体问题、期望行为和范围。

PR 描述应说明解决的问题、最终行为、文档变化和实际运行的验证；未运行的检查写明原因。保留其他人的未提交工作，只纳入与本次任务相关的文件。

提交信息使用简洁标题和正文，标题与正文之间空一行，例如：

```text
docs: 完善项目入门说明

- 补充最小配置与会话操作入口
- 对齐模块文档导航
```

主项目尚未声明开源许可证，第三方材料继续适用各自许可；贡献流程不代替项目的许可声明。
