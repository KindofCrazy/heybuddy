# MemoryFS：Core、Deferred 与索引

## 职责与架构组成

`src/personal_agent/memory/store.py::MemoryFS` 实现 `core/memory.py::CoreMemory.compile()`，并提供有界文件读取、目录元数据、摘要冲突检测和单文件原子替换。`memory/models.py` 定义三个固定 Core 块、`CoreMemoryData`、SHA256 摘要、简化 Frontmatter 解析和输出预算；摘要仅用于当前文件的乐观并发校验，不构成版本历史。`memory/index.py` 维护分层发现链、带校验标记的生成条目及修改后的索引同步；`memory/migration.py` 仅负责一次性 V0.4 导出；`memory/privacy.py` 保留写入前的 Git 隐私检查。`memory/tools.py::register_memory_tools()` 将这些能力接到现有 Registry。

Agent Loop 和 ContextBuilder 不依赖 MemoryFS。前者执行原有工具循环，后者只通过 Protocol 编译 Core。SQLite 不保存长期 Memory 的权威副本，Deferred Markdown 是整理知识的唯一权威源；SQLite messages 仍是原始交流的唯一权威源。

## 文件和链接约定

```text
<memory_root>/<agent_id>/
  persona.md
  human.md
  MEMORY.md
  projects/
    MEMORY.md
    heybuddy.md
```

三个 Core 文件不要求 Frontmatter，升级时不改写旧正文。每个 Deferred 目录都有无 Frontmatter 的 `MEMORY.md`，标题后第一个正文段落作为目录概要。普通知识文件必须用两个非空单行字符串字段：

```markdown
---
name: heybuddy
description: HeyBuddy 的架构、设计取舍和开放问题。
---

# HeyBuddy

当前采用手写 asyncio Harness，并用 SQLite 保存会话和 Recall 证据。
```

这是刻意受限的 Frontmatter 子集，不是完整 YAML：字段只能是 `name`（最多 80 字符）和 `description`（最多 240 字符），支持普通单行文本、JSON 双引号字符串及单引号字符串；不支持注释、列表、多行标量和额外键。用户可直接用 UTF-8 编辑器修改文件。

所有索引链接统一**相对 Agent Memory Root**，不是相对当前 Markdown 文件。只识别代码围栏外的 `- [标签](路径): 说明` 条目。根索引写 `- [Projects](projects/MEMORY.md): 项目架构与设计决策。`；`projects/MEMORY.md` 写 `- [HeyBuddy](projects/heybuddy.md): HeyBuddy 的当前理解。`。每级索引指向直属主题文件或直属子目录索引，不能跳级。生成条目末尾可附带 `<!-- heybuddy-index:<SHA256> -->`，校验的是注释前可见条目的 UTF-8 字节；这仅表示该条目仍保持生成时的文字，不是版本历史、权限或认证。引用式链接、独立 HTML、相对父目录路径和围栏内示例不会构成发现链；无效条目需人工审阅。

Agent ID 限定小写 ASCII 字母、数字、下划线或连字符，拒绝 Windows 大小写别名导致两个身份共用目录。目录和文件名称使用 ASCII 字母、数字、下划线或连字符，主题文件以 `.md` 结尾；正文和元数据支持中文。路径用 `/`，最多三层目录；拒绝空段、绝对路径、`..`、反斜杠、Windows 设备名/ADS/尾点、任意额外根文件及大小写不同的 `memory.md`。`versions/`、`skills/` 和隐藏目录不向模型开放。

## 代码实际运行路径

### 启动和请求

1. `main.run_cli()` 恢复数据库与 SessionManager，通过 `config.memory_root_for()` 选择根目录，再以 `MemoryFS.for_agent(root, agent_id)` 绑定身份。
2. `initialize()` 持短写锁调用 `migration.initialize_files()`。新目录写入三个默认块，旧目录先安全迁移；已完成迁移只读取新 Markdown。缺失部分新 Core 时拒绝自动重置。
3. `SessionManager.context()` 将 MemoryFS 作为 CoreMemory Protocol 传给 ContextBuilder。
4. 每次 `Agent._run()` 调用 Provider 前，`ContextBuilder.for_request()` 执行 `MemoryFS.compile()` → `read()` → `render_memory()`。只读取三个 Core 文件，生成 `kind=agent_core_memory, trust=data_only, blocks=...` 的 user 角色 JSON。Base 仍来自代码。
5. 编译 JSON 包括封装和转义在内按 UTF-8 字节限制，默认 8192；超限拒绝，不截断。自动编译片段不进入 pending 或 SQLite。
6. 模型按根 MEMORY.md 的入口调用 `deferred_read` 读取目录索引，再用 `deferred_list` 查看必要元数据，最后读取有关主题。其他知识文件不自动进入上下文。读取结果是正常 ToolMessage，保留 call_id 并照常保存。

### 局部编辑和整块更新

1. CLI 仅解析本次 `/memory edit <请求>`，将 `memory.read`、`memory.update` 和 `deferred.update` 传给 `Agent.run(permissions=...)`；下一次输入恢复空权限。普通自然语言、历史命令、Core/Deferred/Recall 文本均不能授权。
2. Registry 同时过滤工具定义并在执行前复查权限，再以 ToolArguments 严格校验字段。
3. 模型先读取目标。`memory_read` / `deferred_read` 返回当前 `hash`；修改携带 `expected_hash`，不再使用 `expected_version`。
4. Core 工具仍调用 `MemoryFS.edit()` → `writing()` → `_edit_locked()`。Deferred 工具调用 `index.edit()`，先取得同一个 writing() 短锁，再调用 `_edit_locked()` 完成原有摘要/唯一匹配检查，随后在该锁下维护父索引，避免嵌套锁。old_text 必须非空、精确唯一匹配，连重叠重复也拒绝。
5. 仅替换对应片段。Core 更改先验证三块的总编译容量；普通 Deferred 文件更改验证 Frontmatter 和文件字节上限。
6. `replace()` → `atomic()` 在目标同目录写独占临时文件，flush/fsync，紧接替换前再次核对目标摘要或不存在条件，然后 `os.replace()`。失败不自动重试；遗留隐藏临时文件从不作为记忆读取。
7. 工具返回新 hash，Agent 保存结果后继续。下一次请求重新读取最新 Core，后续 Deferred 读取也立即看到已发布内容。

`memory_update` 是明确的整块替换，只接受固定 Core 块；它不保留被省略内容，应仅用于用户明确要求整块替换。`deferred_edit` 可编辑主题及子目录索引，不能直接编辑根 Core；元数据/概要的父入口同步仍属于 deferred.update 的受控维护范围，可能更新根入口。具体保护策略见下节。

### 创建与索引一致性

1. `index.create()` 验证主题路径、Frontmatter、65536 字节上限及全树预算，取得写锁，拒绝已存在目标。
2. 创建目录并先原子写入知识文件。`ensure_chain()` 从根向下创建缺失目录 MEMORY.md，向上层索引追加目录链接，最后向所在目录索引追加主题链接。新目录统一采用中性概要“此目录整理按主题组织的长期知识；请查看下方索引选择相关主题。”，不从第一个主题推断范围，也不根据目录名称猜测用途。父入口只使用目录自身概要。添加主题不会覆盖已存在的目录说明；用户了解范围后可直接编辑或授权 Agent 将其完善为整体主题范围和读取时机。
3. 新增目录和主题条目均附带内联 heybuddy-index 校验注释。每个索引修改也独立核对摘要和原子替换。现有索引文字完整保留，已有目标链接不重复追加；未闭合代码围栏阻止发现时拒绝假成功。
4. 全部完成才返回 `created=true, indexed=true`。文件已创建但索引步骤失败，返回 `memory_index_incomplete`，明确文件可能存在且未回滚，不能盲目再次创建。
5. `index.inspect()` / `deferred_index_check` 检测缺索引、缺父链接、无效/跳级链接、悬空/重复链接、无效主题元数据，以及可确定陈旧的生成条目。生成标记仍在但文字已改写时返回 review 提示，不把人工描述与 Frontmatter 不同判为错误。扫描拒绝危险路径及超预算树，不会静默跳过。
6. `index.repair()` / `deferred_index_repair` 创建缺失索引、追加缺失链接，并更新校验标记完整的陈旧生成条目；结果分别记录 additions/updates。不删除用户内容，不改写无标记或被人工修改的条目，不猜测悬空链接应该指向哪里。结果 `ok=false` 代表仍有问题；中途失败仍返回明确部分完成错误。修复是显式操作，不在启动或每次请求时自动运行。

### 元数据变更与人工索引的保护

Frontmatter `name/description` 描述主题文件本身；父索引说明可以描述**什么时候应该打开它**，两者无需文字相同。目录 MEMORY.md 描述整体范围，并给直属主题/子目录地图；根 MEMORY.md 让未来 Agent 在不知道文件名时选择第一层入口。

`index.edit()` 比较修改前后的解析结果，而不是只看替换位置：

- 普通正文编辑或仅改变 Frontmatter 的等价引号/格式，metadata_changed=false，index.status=not_needed，不读取或改写父索引。
- name/description 变化时 metadata_changed=true；目录索引首段概要变化时 summary_changed=true。只有相关发现信息改变才维护直属父入口，不扫描或注入整棵树。
- 父入口只有一个且 heybuddy-index 校验值仍与整条可见文字一致时，`maintain_link()` 按新元数据/目录概要更新该条目的字符区间，并更新标记；保留原换行、其余链接及无关文字。缺少目标链接时只补一条；已有重复目标时不猜测保留哪一条。
- 旧 V0.5 无标记条目一律视作人工维护，即使恰好等于旧 Frontmatter 也不自动覆盖。被直接改写的生成条目同样保护；不会仅凭 description 不同认定它陈旧。用户要固定自己的读取时机说明，可去掉该条目末尾的生成注释。
- 不做破坏性迁移：已有目录概要和旧入口不会自动重写或补标记。旧目录曾被首个主题限定时，需用户审阅后明确完善目录概要及父入口。

不能安全同步时，deferred_edit 的外层 ToolResult.ok=false，error.code 为 memory_index_review 或 memory_index_incomplete；data 仍返回已保存文件的新 hash 及结构化状态，例如：

```json
{
  "path": "projects/heybuddy.md",
  "edited": true,
  "complete": false,
  "metadata_changed": true,
  "summary_changed": false,
  "index": {
    "path": "projects/MEMORY.md",
    "target": "projects/heybuddy.md",
    "status": "review_required",
    "reason": "custom_or_unmanaged_link"
  }
}
```

示例省略 hash。review_required 表示需要检查人工/无标记或重复条目；failed 表示索引 I/O、摘要冲突、容量等操作失败，reason 只返回安全错误码。edited=true 明确表示主题或目录文件已成功保存；不得重新应用已成功的编辑，也不声称已回滚。先查看 data.index.path，再在需要时明确编辑人工说明或调用授权修复。缺失索引和链接、完整标记的 stale_managed_link 可由原 deferred_index_repair 修复；人工说明和重复链接需要明确判断，不通过重新创建文件解决。

`inspect()` 的 index_entry_review 只是“标记与已改写文字不符”的提示，不是内容语义错误。无标记的人工说明不参与 Frontmatter 文本一致性比较；检查通过不代表所有概念、用途和文字都经过语义验证。校验标记不是安全签名，持有本机写权限的用户仍可重写它；工具授权和路径隔离独立保持。

## 工具与权限

| 工具 | 默认/所需权限 | 参数重点 |
| --- | --- | --- |
| `memory_read` | `memory.read` | `block`，返回 content/hash |
| `memory_edit` | `memory.update` | `block, old_text, new_text, expected_hash` |
| `memory_update` | `memory.update` | `block, content, expected_hash` |
| `deferred_list` | 默认只读 | `directory=''`, `offset=0`, `limit=10` |
| `deferred_read` | 默认只读 | `path`, `offset=0`, `limit=4096`, 可选 expected_hash |
| `deferred_create` | `deferred.update` | `path`, 带 Frontmatter 的完整 content |
| `deferred_edit` | `deferred.update` | `path, old_text, new_text, expected_hash` |
| `deferred_index_check` | 默认只读 | `offset=0`, `limit=20` |
| `deferred_index_repair` | `deferred.update` | 无参数；补缺项和安全刷新生成条目 |

`memory_history`、`memory_restore`、`memory.restore` 和 `/memory restore` 已移除；强制调用旧工具返回 unknown_tool。旧 memory.write 不能开启写入。可信嵌入入口自行选择本 Run 的最小权限；`deferred.update` 隐含创建时追加根发现链接以及目录概要变更时安全更新根入口的能力。`/memory repair` 是用户直接请求本地补链，不发送模型、不创建聊天 Run，也不改变下一轮权限。

## 大小、隐私与异常边界

- 单个 Deferred 文件最多 65536 UTF-8 字节；Core 总编译默认 8192，可配置 512～1048576。普通 Markdown 根文件只允许固定三个块。
- 目录每页最多 10 条、单目录最多 500 项，全树检查最多 500 个文件/目录（含根）和 500 条索引目标。越界返回明确错误。索引问题每页最多 20 条，通过 next_offset 继续。
- `deferred_read.limit` 是转义后 UTF-8 正文字节预算，范围 6～8192。offset/next_offset 是 Unicode 字符位置；truncated 明确标记正文未读完。续读传上一次 hash 可检测期间变化；每次仍只读取有界文件，不是流式无限读取。
- Memory 工具 data JSON 最多 16384 字节；目录元数据按字节裁短，主题字段有 metadata_truncated 标识；目录概要本身就是摘要。很大配置的 Core 若超过工具输出上限，memory_read 返回明确错误，不能借工具无界输出。
- 写前继续要求 Git 内实际 Agent 目录整目录被忽略、无已跟踪文件；无法验证就拒绝，不改忽略规则/索引。默认新安装存到用户数据目录，旧位置原地迁移。迁移备份和原版本目录同样位于私有 Agent 根。
- `guard()` 检查文件、所有父目录的 Symlink/Junction/Reparse Point，也拒绝硬链接；`raw()` 只读普通文件，防止管道等特殊对象阻塞。隔离依赖宿主绑定根目录，模型没有 agent_id 参数。
- CoreMemoryError 在工具层转为带 call_id 的安全 ToolExecutionError，不输出磁盘路径或异常正文；请求编译错误则返回 memory_error。旧文本、摘要、容量或元数据校验失败不覆盖目标。

受控写入由短文件锁互斥；外部编辑器不参与该锁。摘要检查能发现读取后和临时文件 flush 期间的大多数编辑，但最后一次检查与 os.replace 之间仍有竞争窗口，内容改后又改回也无法仅靠摘要辨别。不要同时用编辑器和 Agent 修改同一文件。多文件操作及三块 Core 的读取没有全组事务快照，可能观察到不同文件各自的最新状态。

同步本地磁盘与 Git 检查阻塞事件循环，协作式 asyncio timeout 无法硬取消这些步骤；不将写操作留到后台线程。单文件原子替换不是断电保证，没有目录 fsync 或网络文件系统承诺，也不是防同 OS 用户主动攻击的安全沙箱。Markdown 发布与 SQLite 工具结果保存没有共同事务；写库失败不撤销文件，不自动重试。正文可能在正常工具审计历史中保留，均为明文，禁止保存凭证。恢复依赖用户备份和一次性迁移备份。

## 阅读与验证

最小本地示例（不需要 API）：

```python
from pathlib import Path
from personal_agent.memory.store import MemoryFS
from personal_agent.memory.index import create, inspect

fs = MemoryFS.for_agent(Path.home() / "heybuddy-data/memory", "demo")
fs.initialize()
create(
    fs,
    "projects/demo.md",
    "---\nname: demo\ndescription: Demo 的设计。\n---\n\n# Demo\n\n轻量 Harness。\n",
)
print(inspect(fs))
print(fs.read_file("projects/MEMORY.md"))
```

`tests/test_memory_store.py` 覆盖外部编辑可见性、摘要冲突、并发受控写入、UTF-8/JSON 容量、路径与链接，以及真实进程在替换前后退出。`tests/test_memory_migration.py` 覆盖旧格式字节保留、备份和各阶段中断。`tests/test_memory_runtime.py` 保留 Core 的授权过期、刷新、Base 隔离和双存储窗口回归。`tests/test_deferred_memory.py` 覆盖分层创建、索引修复、局部更新、分页、安全、CLI 新会话/重启和 Recall 协作；`tests/test_memory_privacy.py` 验证 Git 隐私。运行 `uv run pytest`，均使用临时数据和 Fake Provider，保留网络禁用保护。

V0.5.1 在同一测试文件增加多主题目录中性概要、正文编辑不写索引、元数据/目录概要安全同步、旧版无标记和人工改写保护、LF/CRLF/CR 换行保留、重复链接、父索引冲突、部分结果持久化，以及陈旧生成条目的显式修复。`tests/test_tools.py` 验证结构化失败数据沿用原 JSON 校验契约。

## V0.7 Reflection 的可信写入路径

除 /memory edit 授予本轮受限工具权限外，用户现在可通过显式 /reflect 从原始历史生成待审阅计划。生成/选择/比较阶段只读 MemoryFS；`ReflectionService.apply()` 必须由可信 CLI 带计划 ID 和条目调用，不作为 ToolRegistry 工具。历史与模型输出不能批准。

Core 局部更新仍调用 MemoryFS.edit()，Deferred 更新/创建仍调用 index.edit()/create()；路径、Frontmatter、容量、expected_hash、短锁、Git 隐私和原子发布规则全部保留。计划显示具体修改及自动索引补链范围。编辑器改变目标后拒绝旧计划；索引失败保留已写 hash 与父索引原因，不重试已成功正文。用户可显式修复索引并 reconcile；元数据仍过期不能确认完整成功。

SQLite 只记录候选、批准、片段和状态，Markdown 仍是后续读取的唯一权威源。Reflection 不直接追加原始聊天日志，Compaction 也不触发它。完整调用、部分完成和崩溃窗口见[Reflection](reflection.md)，验证见 tests/test_reflection.py。
