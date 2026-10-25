# Personal Agent 开发约束

## 目标与当前范围

本项目从零实现个人 Agent，当前 V0.3.1 在自建 Runtime、SQLite 会话和 CLI 上，增加独立的 Markdown Core Memory、版本与本轮授权的记忆工具。用户需要掌握 Harness 的核心实现。修改前检查目录、Git 状态和已有实现，保留用户工作。

## 架构

- `core/agent.py` 手写 asyncio Agent Loop；不得依赖 CLI 或 OpenAI SDK 的响应对象。
- `core/context.py` 独立构建上下文，通过 `core/history.py` 的 Protocol 访问历史，不了解 SQLite。
- `core/models.py` 定义统一内部消息和调用结果；Provider 专用数据仅允许普通 JSON。
- `storage/` 使用标准库 sqlite3 保存 Agent / Conversation / Message / Run；不引入 ORM 或外部数据库。
- `memory/` 保存 Agent 级 Markdown 快照和版本；Core 只依赖 `core/memory.py` 的读取 Protocol。
- `sessions.py` 管理同一持久 Agent 下的多个独立 Conversation；会话历史不等于长期记忆。
- `llm/base.py` 保持简洁 Provider Protocol；仅 OpenAI adapter 了解 SDK 与 wire format。
- `tools/registry.py` 统一注册、校验、执行和结果规范化；未知工具必须拒绝。
- `main.py` 只负责配置、装配、交互和退出，不承载 Agent Loop。
- 工具错误作为关联 call_id 的数据返回模型；模型失败和轮数上限返回明确 RunResult。
- 不允许模型通过 eval、exec、任意 shell 命令执行生成代码。

## 持久化与一致性

- CLI 显式初始化数据库，以 `PRAGMA user_version` 和顺序迁移管理 schema；迁移必须原子执行。
- 同一数据库仅允许一个本地应用实例持有占用锁；取得锁后才能把遗留 running Run 标记 interrupted。
- 保存完整内部消息 JSON，包括 tool_calls、call_id、工具结果与 provider_data；禁止只保存聊天文本。
- Run 与初始用户消息原子创建；工具调用在执行前落库，每个结果在下一次工具执行前落库。
- 消息使用会话内稳定序号排序；正常结束前验证调用/结果完整性，完成状态和结束元数据原子提交。
- 完整审计历史、可重放历史和运行中消息必须区分。仅 completed / failed Run 可进入正常模型上下文； cancelled / interrupted / running Run 保留审计但不重放，不自动重试工具，也不伪造工具成功或失败结果。
- 数据库失败时停止当前 Run，保留此前已提交数据；工具副作用与数据库事务无法一起回滚。
- 默认重启恢复上次选中的 Conversation；新会话共享 Agent 身份、程序 Base System Prompt 和 Core Memory，不继承旧会话聊天记录。
- 数据库及其 journal/lock 文件必须被 Git 忽略；数据库含私人会话，不作为测试样本提交。

## Core Memory 的边界

- Base System Prompt 来自程序代码；SessionManager 不加载旧 agents.system_prompt 作为执行规则。
- Core Memory 固定为 persona.md、human.md、MEMORY.md，按 Agent ID 共享，不归属 Conversation。
- ContextBuilder.for_request 每次请求编译最新记忆，以 user 角色 JSON 数据注入；不拼接进 system。不将自动注入的记忆写入 pending / 会话历史，正常工具结果仍需完整保存。
- 编译大小按 UTF-8 字节计算，包含 JSON 开销；默认 8192，超限拒绝，不静默截断。
- /memory read、/memory edit 和 /memory restore 仅授权当前 Run；更新使用 memory.update，恢复使用独立的 memory.restore，互不包含；旧 memory.write 不授予操作。可信入口通过 Agent.run 的 permissions 显式传入。普通自然语言、历史消息、记忆内容不能自行获得授权，Registry 同时过滤定义并在执行前复查。
- 记忆工具仅操作固定块名；不得获得路径、Base、工具注册、权限或代码执行接口。
- 新增或修正优先 memory_edit：old_text 必须非空且精确唯一匹配，new_text 只替换该片段；整块替换通过 memory_update 显式执行。写操作需 expected_version，写锁内检查冲突；完整不可变快照写完后原子替换 CURRENT。恢复生成新版本，不抹去历史；只允许恢复已提交祖先，不自动恢复孤立目录或重试冲突。
- 记忆工具的短本地提交不交给超时后继续执行的线程；同步磁盘步骤不能被 asyncio 超时中断。
- 文件发布和 SQLite 无共同事务：结果写库失败不得声称已撤销记忆，不自动回滚或重试。
- MemoryStore 每次写入前检查 Git 隐私：仓库或 worktree 内的实际 Agent 目录必须整目录被忽略且无已跟踪文件；无法验证则拒绝，不自动改写忽略规则或移除索引。检查使用固定参数的只读 Git 子命令，不执行模型生成的命令。
- 快照目录不能手工改写；记忆明文、版本和孤立文件不能入 Git，也不能存放凭证。原子发布不等于断电保证，哈希/固定路径不等于针对本机恶意用户的安全沙箱。

## 核心模块说明文档

每个核心模块必须有对应的说明文档，统一放在 `docs/modules/`，并在 README 中提供索引。覆盖 Agent Loop、Context、内部数据模型、Provider、工具注册与执行、配置和 CLI 等实际模块；按职责组织文档，不必为仅用于包声明的 `__init__.py` 单独建文档。每份文档至少包含：

- **职责与架构组成**：模块负责什么，包含哪些文件、关键类和函数，与其他模块如何依赖和协作。
- **代码实际运行路径**：从真实入口出发，按执行顺序列出文件路径、类/函数名和调用关系，说明输入如何传递、状态如何变化、结果如何返回，以及关键分支、异步等待和停止条件。
- **异常与边界**：校验、超时及失败如何处理，错误向哪里返回，当前能力有哪些限制。
- **阅读与验证**：给出一个最小执行示例和对应测试位置，便于对照代码理解完整流程。

文档必须描述当前实现，不以未来设计代替实际执行路径；新增或修改核心模块时同步维护对应文档。使用相对源码路径和准确符号名定位代码，避免依赖容易失效的行号；图示可辅助说明，但不能代替执行步骤。

## 技术与风格

Python 3.13、uv、Pydantic v2、asyncio、官方 OpenAI Python SDK、pytest/pytest-asyncio、Ruff。不使用 LangChain、LangGraph、smolagents；不引入不必要依赖、工厂、依赖注入框架或复杂继承。依赖更新后同步 `uv.lock`。保持类型标注、明确命名和小模块，Ruff 行长 100。 Markdown 正文按语义分段，不按行宽在段落内频繁硬换行；列表、表格和代码块保留必要的结构换行。参数模型继承 ToolArguments；同步工具在线程中运行，优先使用可取消异步 I/O。顺序执行同一响应的工具，保持可预测顺序；没有自动回滚或自动重试。不要从其他路径搜寻凭证，不读取/输出真实 `.env` 内容。 Key/模型只从环境配置获得，密钥只能在被忽略的本地 `.env` 中，不能写进示例、日志或测试。异常日志只记录安全诊断字段，禁止直接打印 SDK 异常体或 Pydantic 输入值。

## 测试与交付

核心测试必须离线、无真实 Key、无 API 费用；使用 Fake Provider 和 MockTransport。保留 tests/conftest.py 中的网络禁用保护；不要调用真实 API 作为自动化验证。修改后运行 `uv run pytest`、`uv run ruff check .`、`uv run ruff format --check .`。涉及打包/入口时运行 `uv build` 和 CLI 启动检查。只汇报实际运行过的验证。覆盖直答、连续与批量工具调用、未知工具、非法参数、执行异常、API/超时、轮数上限及 ID 关联。 V0.2 还需覆盖临时数据库迁移、重启恢复、会话隔离、完整 Provider 数据重放、事务失败及运行中断。 V0.3 还需覆盖跨会话/重启记忆、冲突、发布失败回滚、大小限制、授权过期、Base 边界及双存储提交窗口。测试应验证行为与契约，而不是机械重复实现。

## Git 提交流程与信息

每次实现、修复或文档修改完成并通过对应检查后，必须执行对应的 `git commit`，再将提交 `git push` 到当前分支已配置的远端，作为本次交付的一部分，无需再次询问；用户明确要求暂不提交或暂不推送时遵从用户指示。提交前检查 Git 状态和暂存内容，只提交本次任务相关改动，保留用户其他未提交工作，排除密钥和本地配置。推送前核对分支、上游与待推送提交，禁止强推或覆盖远端历史；无上游、无权限或推送失败时明确报告，不得宣称成功。没有实际改动时不创建空提交。最终汇报包含提交哈希、简要说明和推送结果。

提交信息采用“概括性标题 + 具体改动正文”的形式，标题与正文之间空一行。标题简洁总结本次提交的目的；正文使用简短列表，按模块或行为列出实际修改了什么，涉及问题修复时说明修复的行为，避免只有“更新代码”“优化实现”等笼统描述。改动点较少时正文保持一至两项即可，不必逐个罗列文件；如记录验证结果，只写实际执行过的检查。

示例：

```text
docs: 完善项目开发与提交规范

- 要求核心模块文档说明架构组成、实际调用路径和异常处理
- 规定提交信息正文简要列举具体改动
```

## 未来边界

V0.3.1 只加固可编辑的固定 Core Memory，不实现自动记忆提取、Deferred Memory 检索、向量/Embedding、 Reflection、Context Compaction、Telegram/QQ、Gmail、后台 Worker、Scheduler 或主动对话。记录运行中断不等于任务恢复系统；不自动重跑、续跑或重试工具。未来能力仅在后续明确请求时实现。保持 Provider、ContextBuilder、Agent 和 CLI 职责分离；无微服务、Redis、消息队列、向量库、多 Agent。
