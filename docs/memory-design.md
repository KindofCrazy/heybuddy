# V0.5 设计调研：借鉴 Letta Code 的 Memory

## 调研基线

阅读用户提供的本地 Letta Code checkout，提交 `6c8b08fedfb6f3fb437163ede8320a387151693f`。下面链接固定到该提交，避免把之后的上游行为当成本次依据。也查看了 [Letta Code 项目](https://github.com/letta-ai/letta-code) 的公开说明；实现决策以源码为准。

| 源文件 | 实际职责与发现 |
| --- | --- |
| [maintaining-core-memory/SKILL.md](https://github.com/letta-ai/letta-code/blob/6c8b08fedfb6f3fb437163ede8320a387151693f/src/skills/builtin/maintaining-core-memory/SKILL.md) | Core 放每轮需要的人格、用户模型和原则；编辑应整合已有原则，根 MEMORY.md 解释何时打开各 Deferred 目录 |
| [maintaining-deferred-memory/SKILL.md](https://github.com/letta-ai/letta-code/blob/6c8b08fedfb6f3fb437163ede8320a387151693f/src/skills/builtin/maintaining-deferred-memory/SKILL.md) | Deferred 保存跨事件形成的理解而非日志；目录 MEMORY.md 给概要和逐级地图，普通文件用 name/description；链接相对 Memory Root |
| [memory-filesystem.ts](https://github.com/letta-ai/letta-code/blob/6c8b08fedfb6f3fb437163ede8320a387151693f/src/agent/memory-filesystem.ts) | `getScopedMemoryFilesystemRoot()` 区分身份/后端；`ensureLocalMemfsCheckout()` 在 local backend 初始化 Git，远端模式 clone/pull；树展示有行/字符/子项限制 |
| [memory-format.ts](https://github.com/letta-ai/letta-code/blob/6c8b08fedfb6f3fb437163ede8320a387151693f/src/agent/memory-format.ts) | `isCoreMemoryPath()` 在 v2 把根 Markdown 视为 Core；`isProjectedMemoryPath()` 检查每一级目录都有 MEMORY.md，skills 单独处理 |
| [system-prompt-compilation.ts](https://github.com/letta-ai/letta-code/blob/6c8b08fedfb6f3fb437163ede8320a387151693f/src/backend/local/system-prompt-compilation.ts) | local backend 的 `collectCommittedMemoryFiles()` 从 Git HEAD 枚举/读取；`renderRootMemoryProjection()` 将根 Core 与顶层目录入口编入 system；不把所有 Deferred 正文载入 |
| [memory-git.ts](https://github.com/letta-ai/letta-code/blob/6c8b08fedfb6f3fb437163ede8320a387151693f/src/agent/memory-git.ts) | `initializeLocalMemoryRepo()` 初始化仓库，`commitMemoryWrite()` 提交选定路径，`pullMemory()`/`pushMemory()` 和 post-turn 同步处理远端与分歧 |
| [letta_root_memfs.md](https://github.com/letta-ai/letta-code/blob/6c8b08fedfb6f3fb437163ede8320a387151693f/src/agent/prompts/letta_root_memfs.md) | 区分 recall 经验与可编辑的长期理解；根索引驱动未来发现，目录索引驱动 Progressive Disclosure；同时包含 HeyBuddy 不采用的自主学习/多会话协作约定 |
| [memory-worktree.ts](https://github.com/letta-ai/letta-code/blob/6c8b08fedfb6f3fb437163ede8320a387151693f/src/agent/memory-worktree.ts) 与 [memory-git-hooks.ts](https://github.com/letta-ai/letta-code/blob/6c8b08fedfb6f3fb437163ede8320a387151693f/src/agent/memory-git-hooks.ts) | `createReflectionMemoryWorktree()` 从 base HEAD 建独立临时分支与工作目录；合并流程与校验 hook 配合；post-commit hook 避免临时 Reflection 分支推送至用户远端 |

## 实际实现、已有能力与采用范围

| 问题 | Letta Code 实际设计 | HeyBuddy V0.4 已有 | V0.5 选择 |
| --- | --- | --- | --- |
| Core 为何常驻 | 身份、用户模型、原则和发现入口应始终可见，根 Markdown 编入 system | 固定三块，每次请求以低信任 user JSON 编译 | 保留三块、数据优先级、JSON UTF-8 总预算和每次刷新；不把 Memory 提升为 system |
| Deferred 存什么 | 当前主题理解、分析、跨多次经历提炼的知识 | 无 Deferred | 普通 Markdown 按主题整理，优先编辑旧主题；不自动复制聊天 |
| Recall 的职责 | 原始经验另存并查询，索引知识不重复保存事件日志 | SQLite messages + Agent 绑定 FTS5，completed/failed 可见 | 完全保留这条独立证据路径，需要验证旧讨论时搜索它 |
| 未来怎么发现 | 根 MEMORY.md 描述何时打开目录；每级 MEMORY.md 给概要与地图 | MEMORY.md 只是 Core 块 | 创建时逐级补链；读取目录元数据后再选正文；显式检查/修复孤立和悬空引用 |
| name/description | name 是主题名，description 描述文件内容，父索引解释何时读取 | 不要求 Frontmatter | 只在普通 Deferred 文件要求两个简短单行字段；旧 Core 原文不变 |
| Prompt 初始内容 | Git HEAD 的根 Core，以及顶层索引路径投影 | Base、三块 Core、当前会话 | Base、三块 Core（根索引已经在其中）、当前会话；不重复注入一个 Deferred 全树，不自动搜索 |
| 历史和协作 | Git 提交提供变更历史；分支/worktree 隔离并发和 Reflection，再合并/同步 | 不可变 Snapshot、CURRENT、Revision 与 Restore | 移除正常版本链；保留当前内容 hash、短锁和单文件 replace，恢复交给备份 |

## 为什么不复制 Git-backed MemFS

上游 Git 不只是 Markdown 保存容器：prompt compilation 以已提交 HEAD 为边界，hooks 检查数据，worktree 给并发与 Reflection 隔离工作区，pull/push 处理远端同步。这些条件能解释它的复杂度，但 HeyBuddy 当前只有本地单用户、顺序工具调用，没有 Reflection、并发任务或远程 Memory 同步需求。引入 Git 分支、提交、认证和合并会模糊 Harness 的学习边界，且让编辑器更改必须经过提交才能生效。

原 Snapshot Store 同样超出了当前用途：每改一个小片段都要复制三个块、写 revision.json 再更新 CURRENT；不可变快照阻止用户直接编辑，并使 History/Restore 与文件查看纠缠。V0.5 直接读取普通文件，摘要只校验过期修改；不用另一套数据库替换旧版本系统。

## 单文件可靠性与多文件发现链

`MemoryFS.atomic()` 用目标同目录临时文件 + flush/fsync + 替换前摘要检查 + os.replace。受控写入共用已有 FileLock，不加事务框架。编辑器不参与锁，最后检查与替换之间仍有竞争窗口，文档明确这一限制。

创建先保存主题，再逐个创建/更新各级索引；所有步骤完成才报告成功。中断可能留下孤立主题或缺链，返回明确部分完成错误，`index.inspect()` 可定位，授权 `index.repair()` 只追加缺项。悬空/无效链接不会被自动删除，不通过牺牲用户文字“修复一致性”。

旧格式迁移只保留一次性的读取与哈希验证代码，校验 CURRENT → 完整唯一备份 → 导出相同字节 → 标记迁移完成；中断有可重复、拒绝覆盖人工编辑的路径。正常运行没有第二套存储后端，见[迁移说明](memory-migration.md)。

## 模块调整与明确不做

修改 `memory/store.py`、`models.py`、`tools.py`，增加小模块 `index.py` 和仅迁移用的 `migration.py`；继续复用 `privacy.py`、`file_lock.py`。配置处理新安装的用户数据位置与已知旧位置，CLI 调整本轮授权和本地索引维护命令，Base Prompt 描述层次与内容原则。ContextBuilder 的 compile Protocol、Agent Loop、ToolRegistry 执行机制和 SQLite schema/Recall 保持职责不变。相关测试和模块文档同步更新。

不实现 Git-backed Memory、History/Restore、分支/worktree、远程同步、Reflection/Sleep Agent、后台任务、Scheduler、自动知识提取、全历史总结、Embedding/Vector DB、Context Compaction 或新的通信入口。未来只在明确需求出现后另行设计。
