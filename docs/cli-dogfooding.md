# CLI 使用与检查指南

先在被忽略的本地 `.env` 配置模型/密钥和真实 context window；不要在命令行粘贴 Key。建议使用独立试用数据库及源码仓库外的 Memory，避免与日常私人会话混合。以下环境变量只影响当前 PowerShell；在同一个窗口重启会继续相同数据。

```powershell
$env:AGENT_DATABASE_PATH = "$env:LOCALAPPDATA/HeyBuddy/dogfood/heybuddy.sqlite3"
$env:AGENT_MEMORY_ROOT = "$env:LOCALAPPDATA/HeyBuddy/dogfood/memory"
$env:AGENT_MEMORY_AUTOMATION = 'suggest'
uv run personal-agent
```

先观察普通聊天与自动检查的额外等待：

```text
/memory automation off
Python 的 tuple 是否可变？请一句话回答。
/memory automation suggest
以后解释代码，请先讲清为什么这样设计，再给实现细节。这是我确认的长期偏好。
/memory automation status
/reflect review
```

Agent 答案先出现，随后 Memory 检查提示；下一次输入会等待检查结束。无价值内容通常只增加一次 Worthiness；有价值时最多再进行 Reflection 三阶段。status 分开显示自动 requests、input/output/reasoning、seconds、trigger/plan 和 pending/unprocessed；reasoning 包含在 output 中，usage_complete=false 表示计量不完整，不是零成本。普通 Run 用量在 Journal 的运行结果中，自动部分另存在 schema 5 automation_batches，不把 Compaction 算作自动记忆请求。

review 是私人数据：核对用户原话/来源、目标文件、局部 Diff、是否扩写了未确认偏好、hash 与索引影响。只有内容准确才应用；将下面占位符替换为 review 显示的真实值。

```text
/reflect apply <plan_id> <item_id>
/reflect reject <plan_id> <item_id>
/reflect skip <plan_id> <item_id>
/reflect status
/new
我对解释代码有什么长期偏好？
```

apply/reject/skip 可用 all，但建议逐条检查。三种操作是选择关系，不应对同一已终结条目连续执行。新 Conversation 不继承旧聊天，只共享 Core/主题索引；成功 apply 后下一请求加载最新 Markdown。退出再 `uv run personal-agent`，用 `/reflect review <plan_id>` 查看已有计划，不会重新生成或自动写入。

检查项目更新/重复：先经批准建立一个项目主题，再明确更改一项长期决策，检查提案是否更新原文件并保留其他段落。重复说相同事实时应 no-op 或阴性判断。`假设我住北京，请分析出行方案` 不应变成“用户住北京”。当前真实评测仍发现过偏好扩写、human.md 误选 create；不要跳过 review。

已有待审计划时继续聊天，新历史仍落库但不重复提取；status 显示未处理 Run。处理完所有条目后，下一次 completed 输入或显式 `/memory automation run` 继续处理。失败不会改变已显示的答案；先检查安全 error_code，再显式 `/memory automation retry`。旧工具不会自动重跑。若单 Run（含 Provider JSON）超自动源页上限，会保留未处理状态；不要期待无限加预算或自动截断成功。

外部编辑器修改已规划的 Markdown 后 apply 应冲突停止；不要用旧 hash 覆盖。文件可能已保存但索引/记账失败时，先 `/reflect review`，必要时 `/reflect reconcile <plan_id> <item_id>` 只读核对，再 `/memory check`。不要盲目 retry 发布。`/context status` 查看上下文预算/摘要边界，`/status` 查看当前会话与 Run；Recall 应只查询历史证据，不能授予写权限。

auto 是宿主事前批准精确 Markdown SHA256 的小型 knowledge/ 新文件发布，不是通用自主写入；没有 AGENT_MEMORY_AUTO_CREATE_GRANTS 时仍需 review。Core、已有主题更新、重大纠正都需要人工批准。日常试用建议保持 suggest，模式 CLI 切换只当前进程生效。

建议记录：问题类别、是否查对主题、无关/重复工具、Worthiness 是否误触发、提案是否忠实、自动额外耗时与已知 tokens、重启与冲突结果。只分享脱敏诊断，不上传真实 SQLite、计划正文、Memory 或 `.env`。后台任务、Scheduler 和自动续跑均未实现。
