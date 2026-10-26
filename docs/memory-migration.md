# V0.4 → V0.5 Memory 迁移

V0.5 用 Agent 根目录的三个普通 Markdown 文件替代 CURRENT/Snapshot/Revision。SQLite schema 仍为 2，会话消息和 Recall 不做格式转换。升级不会改写记忆正文，不会自动清理旧版本，也没有新的 History/Restore 机制。

## 启动位置

显式 `AGENT_MEMORY_ROOT` 优先，实际目录仍为 `<root>/<agent_id>`。没有该配置时，`config.memory_root_for()` 先检查当前数据库已知的 `<database_path>.memory/<agent_id>`；目录已存在就沿用，包括迁移完成后的重启。如果没有旧位置，新安装默认使用 Windows `%LOCALAPPDATA%/HeyBuddy/memory/<agent_id>`，其他系统 `~/.local/share/heybuddy/memory/<agent_id>`。CLI 的 Agent ID 仍是 heybuddy；嵌入入口使用小写 Agent ID，避免大小写不敏感文件系统的身份别名，非小写 ID 会明确拒绝，不自动重命名旧目录。更换数据库路径、工作目录或根配置会改变位置；程序不扫描其他目录寻找私人数据。

在有数据的环境升级前，建议自行备份数据库及整个 Agent Memory Root。新默认位置不会自动搬走已存在的旧目录。自定义目录位于源码仓库时，实际 Agent 目录必须整目录被忽略且无已跟踪文件，否则在迁移前拒绝写入；不会自动改 Git 配置。

## 实际迁移步骤

`main.run_cli()` → `MemoryFS.initialize()` → 写锁下 `memory/migration.py::initialize_files()`：

1. 检查链接边界；检测 `.migration-v05` 收据与 CURRENT/versions。首次旧迁移读取 CURRENT，仅接受 32 位十六进制版本名。
2. `legacy_blocks()` 读取该版本 revision.json，校验 version、三个固定块的集合及每个 SHA256，按 UTF-8 解码。只迁移 CURRENT 指向的已提交内容，绝不选“最新目录”或孤立版本。
3. 检查新目标文件：已有文件必须与待导出的对应原文逐字节相同；不同就返回 migration_conflict，不覆盖任何目标。先验证新 Core 编译预算，超限保留全部旧数据并停止。
4. `backup_legacy()` 在 Agent 根建立唯一 `.migration-backup-v04-<UUID>/`，复制整个 CURRENT 和 versions，包括孤立目录。复制完成后写 `.backup-complete`；原文件始终保留。备份失败不开始导出。
5. 原子写 `.migration-v05`，记录备份名及 `complete=false`。它仅是一次性迁移收据，不保存事实、版本链或索引。
6. 对缺失的 persona.md、human.md、MEMORY.md 逐个同目录临时写入后原子替换。保留原始 UTF-8 字节、CRLF、空白、旧 Frontmatter 或任意正文，不添加新 Frontmatter。
7. 三个文件都完成后原子将收据标为 complete=true。之后正常运行只使用新 Markdown，原 CURRENT 即使损坏也不会被重新编译。

正常工具参数由 `expected_version` 改成目标文件的 `expected_hash`（64 位 SHA256）。摘要通过 memory_read/deferred_read 返回，不是可恢复版本 ID。Core 自动注入仅包含三个块和数据标记，避免新元数据扩大旧记忆的编译开销。

## 中断与重启行为

| 情况 | 再次启动的行为 |
| --- | --- |
| 备份过程中失败，尚无收据 | 原数据保持；下次建立另一个唯一备份，残留备份不覆盖、不自动删除 |
| 备份完成但导出中断，complete=false | 从收据指定的完整备份校验源；已导出的同内容文件保留，只补缺失文件 |
| 中断后用户修改了已导出的新文件 | migration_conflict；保留用户编辑及所有旧数据，需要人工选择后再启动 |
| 三个文件完成但最后收据写失败 | 下次核对相同内容后完成收据，不重新覆盖文件 |
| complete=true | 只读取新文件；不会把编辑器更改恢复成旧 Snapshot |
| 无收据但同时存在不同的新旧内容 | 拒绝覆盖；先由用户备份、比较并决定要保留哪套内容 |
| versions 存在但 CURRENT 缺失/非法，或哈希错误 | 拒绝重置或猜测恢复，不导出默认记忆 |
| 新格式缺少部分 Core 文件 | incomplete_memory；从备份或编辑器恢复缺少的文件，不自动重建覆盖 |

迁移正在恢复时，请不要修改迁移备份。它是这次导出的固定源，不能作为日常 Memory 编辑入口。收据和 `.backup-complete` 不代表针对本机恶意用户的验证系统，也不提供断电级多文件事务。

## 人工处理与清理

迁移完成后，直接编辑 Agent 根下三个 Core 文件或索引知识文件；旧版本目录不再读取。保留 `.migration-v05`，尤其是尚保留 CURRENT/versions 时，删除收据可能导致新旧内容冲突。确认备份可用且新系统正常后，可由用户手动清理旧 CURRENT、versions 和迁移备份，程序不会代为删除。恢复一个旧块时，由用户从备份复制想保留的文本到根文件；不存在 `/memory restore` 或伪装的版本恢复工具。

若发生 migration_conflict，应先另存用户修改，比较根文件与备份的 CURRENT 对应文件。需要继续原迁移时，只将冲突目标恢复为备份中的相同字节，再重新启动；不要直接重写 versions、CURRENT 或收据来绕过校验。若选择完整的新格式，则由用户在完整备份后移出旧格式及未完成收据，保留三个已确认的新文件。程序不会自行选择。

索引是后续显式维护任务：手工创建 Deferred 文件后运行 `/memory check` 查看问题，`/memory repair` 追加缺失目录索引和链接。悬空链接和无效内容需要人工处理，不会被静默删除。

## 验证

`tests/test_memory_migration.py` 使用 V0.4 revision.json 与原字节哈希格式的合成数据，测试完整备份、字节保持、重复启动、源损坏、不同目标和各中断点。`tests/test_deferred_memory.py::test_cli_create_restart_progressive_read_recall_and_expired_grants` 用 Fake Provider 验证升级后的 CLI 会话和重启读取。真实用户私人数据不作为测试样本，不调用真实 API。
