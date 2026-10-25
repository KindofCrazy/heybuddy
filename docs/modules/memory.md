# Editable Core Memory

## 职责与架构组成

- `core/memory.py`：CoreMemory 读取 Protocol 和安全 CoreMemoryError，隔离 Harness 与文件实现。
- `memory/models.py`：固定 BlockName、MemoryRevision、MemorySnapshot，以及 render_memory JSON 编译。
- `memory/store.py`：MemoryStore 的读取、更新、版本链和恢复；单指针原子发布。
- `memory/tools.py`：四个 Pydantic 参数模型和 register_memory_tools，限制可访问文件和操作。
- `file_lock.py`：通用 OS FileLock，SQLite 的 DatabaseLock 复用它，分别转换领域错误。

三块初始内容只有标题：persona.md、human.md、MEMORY.md。它们共同组成一个版本，每次更新一个块时也创建完整三块快照。Core Memory 属于 Agent，Conversation History 仍属于会话。没有自动总结、记忆提取、目录递归或 Deferred Memory 检索。

## 磁盘模型与版本

```text
<root>/<agent_id>/
  CURRENT
  .write.lock
  versions/<uuid_hex>/
    persona.md
    human.md
    MEMORY.md
    revision.json
```

默认 root 是数据库路径字符串加 `.memory`；AGENT_MEMORY_ROOT 可设置独立路径。版本只允许 32 位小写十六进制；Agent ID 只允许字母、数字、下划线和短横线。调用者不能用文件名传入路径，Store 和工具参数层均有固定 allowlist。 Store 检查受控路径上的符号链接/junction，不提供对抗同一 OS 用户修改祖先目录的隔离。

MemoryRevision 记录 version、parent、created_at、action、restored_from 和各块 SHA-256。 CURRENT 及元数据都是普通文件；哈希用于发现损坏，不是防止本机攻击者伪造版本的签名。版本目录视为不可变，不能直接编辑 Markdown；通过 update API 或授权工具修改。

## 代码实际运行路径：初始化、请求和更新

1. `main.run_cli()` 在恢复持久 Agent 后调用 `MemoryStore.for_agent(root, agent_id)` 和 initialize()。新目录发布初始版本；已有 CURRENT 时完整校验。缺失指针却存在版本时拒绝自动重置。
2. SessionManager 保存读取接口，context() 构造 ContextBuilder(memory=...)。
3. `Agent._run()` 每次调用 Provider 前执行 `ContextBuilder.for_request(messages)`。 `MemoryStore.compile()` 读取一次 CURRENT → `_load(version)` → 校验元数据/哈希/字节数 → render_memory。
4. 编译结果作为 user 角色 JSON 数据插在 Base 后面，不加入工作消息链、pending 或 SQLite。每轮重新读取，所以工具更新后下一轮即看到新版本。
5. CLI `/memory edit <请求>` 调用 `Agent.run(原始输入, permissions={read,write})`。 Agent 过滤模型可见工具，先持久化模型调用，再执行 Registry。
6. Registry 先查工具与 required_permission，再严格校验参数，调用工具包装协程。 memory_update 将固定块名、完整替换内容和 expected_version 传给 Store.update()。
7. Store 取得短期 OS 写锁，在锁内重新 read 当前版本；expected_version 不符返回 memory_conflict。
8. `_publish()` 生成新 ID，在新目录写完整三块和 revision.json，各自 flush/fsync。写并 fsync 临时 CURRENT，最后 os.replace 替换 CURRENT。这个指针替换是提交点。
9. 工具返回新版本，Registry 生成保留原 call_id 的 ToolResult，Agent 保存结果再进入下一轮。下一次更新必须用新版本，不能继续使用该 Run 开始时的旧版本。

读者不需要写锁：只读取一次指针，随后从同一不可变目录读取，所以只能看见完整旧版本或新版本。写锁由 Windows msvcrt / POSIX flock 实现，崩溃时 OS 释放；锁文件存在不等于锁被占用。竞争写者返回 memory_busy 或在取得锁后发现 memory_conflict，不等待后自动覆盖。

## 工具与授权

| 工具 | 参数 | required_permission |
| --- | --- | --- |
| memory_read | block | memory.read |
| memory_history | limit（默认 10，1～20） | memory.read |
| memory_update | block、content、expected_version | memory.write |
| memory_restore | version、expected_version | memory.write |

`/memory read` 本轮只开启前两项，`/memory edit` 开启全部四项。普通输入没有这些权限，即使模型强行请求也返回 permission_denied。授权来自宿主本次调用参数，不从自然语言、持久历史或 Memory 内容推断。工具参数不得包含额外权限字段，执行错误以安全错误码返回，不打印内容或原始文件系统异常。

Base System Prompt 为程序规则，Memory 只是较低优先级参考数据。 JSON 序列化防止记忆伪造消息结构，工具参数不提供系统提示词或注册表修改接口。模型可能仍受恶意文本影响；程序侧权限与文件边界保持独立执行，不能用提示词代替校验。用户应只授予本轮所需权限，不在记忆保存凭证。

## 恢复与失败边界

history() 从 CURRENT 沿 parent 读取已提交元数据，限制返回数量并检测循环。 restore() 在写锁内校验 expected_version，只接受当前已提交链的祖先版本，读取目标完整快照后发布新版本，parent 指向恢复前版本，restored_from 指向目标。不移动指针删除历史，不允许恢复失败留下的孤立目录或其他 Agent 的版本。

| 情况 | 行为 |
| --- | --- |
| 超过大小限制 | memory_too_large；拒绝更新/请求，不截断 |
| 版本过期 / 同时写入 | memory_conflict / memory_busy；不自动重试 |
| 指针、元数据或哈希损坏 | invalid_memory；不默默初始化空记忆 |
| 发布前文件写入或指针替换失败 | 旧 CURRENT 与旧历史不变，返回安全错误 |
| 发布后进程退出 | 新版本已提交，重启读新快照 |
| 记忆成功但 SQLite 工具结果失败 | 新记忆保留，Run interrupted，不自动回滚或重跑 |

大小限制按 **render_memory(snapshot).encode('utf-8')** 长度计算，包含三个块、JSON 字段、版本与转义开销；默认 8192 字节，配置允许 512～1048576。缩小配置后，旧超限版本不可加载/恢复，metadata 历史仍可读。

Memory Tools 的协程直接执行短同步文件提交，没有 await 点，不把写操作放到线程里。这避免“超时已返回、后台线程继续写记忆”，但同步 fsync 可能阻塞事件循环，asyncio timeout 无法打断。原子指针保障当前进程崩溃测试中的一致视图，不承诺断电级持久性或网络文件系统语义。尚无目录 fsync、版本保留/垃圾回收、自动备份或加密。失败前新建的孤立版本保留供人工诊断。首次发布失败后若留下版本而没有 CURRENT，下一次初始化会失败关闭，需要人工检查，不自动恢复。

## 阅读与验证

```python
from pathlib import Path
from personal_agent.memory.store import MemoryStore

store = MemoryStore.for_agent(Path(".heybuddy/manual-memory"), "heybuddy")
initial = store.initialize()
changed = store.update(
    "human.md", "# Human\n用户喜欢简洁中文。", expected_version=initial.revision.version
)
restored = store.restore(initial.revision.version, expected_version=changed.revision.version)
assert restored.blocks == initial.blocks
```

`uv run pytest tests/test_memory_store.py tests/test_memory_runtime.py`：覆盖原子发布前后真实进程退出、并发冲突、失败回滚、版本恢复、Unicode/JSON 大小边界、跨会话与多次重启、同一 Run 更新刷新、授权过期、Base 隔离、路径和参数校验、双存储失败。不读取真实配置、不访问外网；Windows 无创建符号链接权限时仅跳过相应 OS 用例。
