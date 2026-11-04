# Memory：长期理解、证据与写入边界

## 职责与架构组成

`src/personal_agent/memory/store.py::MemoryFS` 管 Agent 级普通 Markdown；memory/index.py 管分层索引，memory/safety.py 与 privacy.py 管路径和 Git 隐私，memory/tools.py 暴露受限工具。memory/archival.py 提供统一只读入口。Core 仅依赖 core/memory.py Protocol。

| 内容 | 权威源 | 何时进入上下文 |
| --- | --- | --- |
| human.md | Markdown 用户资料 | 每次请求 |
| persona.md | Markdown 相处记忆与真实互动反思 | 每次请求，角色模式过滤旧静态人格 |
| 根 MEMORY.md | Markdown 主题索引 | 每次请求 |
| Deferred 主题 | Markdown 与目录 MEMORY.md | 沿索引按需工具读取/检索 |
| Conversation Recall | SQLite 原始已结束聊天文本 | 按需证据搜索 |
| Episode/Thread | SQLite 派生组织 | 相关性检索选入 |
| Context Summary | Conversation 派生摘要 | 有界历史重放 |

persona.md 不等于角色 persona.json；后者是宿主只读角色设定。Summary、Episode、Thread 不成为长期事实的另一份权威库。

## 主题目录怎样被发现

```text
Agent Memory Root/
  human.md
  persona.md
  MEMORY.md                 # 根索引
  projects/
    MEMORY.md               # 目录概要与子项链接
    example.md              # name/description Frontmatter + 正文
```

索引 bullet 链接相对 Agent Memory Root，而不是相对每一级目录。读取时先根据根索引找到相关目录，再按需展开主题，不猜文件名或扫描无关内容。`archival_memory_search(query="index")` 读取根索引，`directory=""` 列根目录，`path="projects/example.md"` 分页读主题；每次只选择一种操作。返回 next_offset 时按原 hash 续读，文件变化应重新核对，不拼接不同版本。

创建文件与补齐索引不是一个跨文件事务。新目录概要保持中性，不拿首个主题的描述冒充整个目录；修复索引要显式调用受限路径，不能因为一次读取发现缺项就自动改写用户文件。

## 机制流程

1. 把用户资料、真实相处认识和主题索引作为每轮参考；主题正文按需读取，原始聊天证据另查历史。
2. 新信息不会仅因出现在对话里就自动成为长期事实：主执行写入需要本轮宿主权限，后台学习使用独立受限策略，手动反思需要审批。
3. 写入前读取目标内容、核对证据和版本，只修改明确片段，保护无关内容与人工整理。
4. 先安全发布文件，再维护发现索引和执行回执；不同存储无法一起提交，部分成功必须如实呈现。
5. 发生冲突或完成未知时先核对现状，不盲目重做。长期知识仍以普通 Markdown 为准，摘要不成为另一份事实库。

## 代码实际运行路径

1. open_application 根据 Agent ID 定位 MemoryFS，initialize 创建三个 Core 或做旧格式迁移；新数据默认在用户数据目录。迁移详见[专用说明](../memory-migration.md)。
2. ContextBuilder 每次 _injections 调用 memory.compile：读取三个固定块，连同 JSON 开销按 UTF-8 字节校验，默认最多 8192 字节。以 user JSON 注入，不混入 system、不写入 Journal。
3. 普通主题读取通过 archival_memory_search 等统一入口；统一入口只管理主题搜索、文件分页、目录与索引检查；Recall 仍通过独立 recall_search/recall_get 查原始聊天，Episode/Thread 由 Working Context Retrieval 选择。旧分立主题读取工具仅为兼容/离线 Harness 保留，不能把这些入口混为全库搜索。
4. Main Agent 写入仅在可信入口逐 Run 授予 memory.update/deferred.update 等权限后可用；自然语言“记住”本身不会授予写工具。局部修改优先 memory_edit/deferred_edit，要求 expected_hash 和精确唯一非空 old_text；memory_update 是显式整块替换。
5. MemoryFS 在短锁内检查路径、大小、隐私与 hash，用同目录临时文件 flush/fsync 后原子发布。创建主题要维护父索引，元数据变化只刷新仍完整的生成条目，保留人工说明。正文修改不重写索引描述。
6. 手动 Reflection 先验证实际用户引文、读现有 Memory、生成具体 Diff，可信审批后才 Apply。P2 Learning 是另一条 Host 授权的后台自动写入路径，复用受限 Writer；可写有真实证据的相处记忆，不能改角色包。Curator 不改相处记忆块。
7. 文件成功而 SQLite 回执失败时保留不确定状态；reconcile 只读核对已发布内容，不盲目重放旧计划。P2 开启时暂停旧 Automation，关闭后才按原 off/suggest/auto 配置运行。

## 异常与边界

拒绝绝对路径、穿越、跨 Agent、链接/Junction 与平台名绕过。每次写前验证实际 Agent 目录在 Git 中整目录忽略且无已跟踪文件；无法确认就拒绝，不自动改忽略规则。不要存凭据。

hash 冲突不自动合并或重试。文件发布与 SQLite 无共同事务；索引失败时即便工具 ok=false，正文也可能已经保存，错误 data 给出路径/hash/原因。单文件原子替换不代表断电保证，也不是针对本机恶意用户的安全沙箱。Markdown 可直接被编辑器修改，程序无日常版本链或 Snapshot。

## 阅读与验证

从 `/memory read`（本轮可信授权入口）或已有主题的只读工具调用开始，查看下一次 prepare 的编译内容。`uv run pytest tests/test_memory_store.py tests/test_deferred_memory.py tests/test_memory_runtime.py tests/test_relationship_memory.py tests/test_archival_memory.py`。自动写入另见 [Learning](learning.md)、[Curator](curator.md)、[Reflection](reflection.md)。

## 未来改进

- 加强每个长期结论到原始用户证据的可检查引用，区分确认事实、过时信息与待核对认识。
- 评测索引/主题规模增大时的检索召回与上下文成本，先验证需求再考虑额外索引技术。
- 统一呈现正文已发布、索引待修、回执未知的部分成功状态，使用户能安全处理冲突。
