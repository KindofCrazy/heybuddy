# Miru 记忆架构及可借鉴部分

## 分层与存储

本说明依据本机 Miru 源码，不读取其私人数据或启动模型调用。

| 层次 | 实现 | 职责 |
| --- | --- | --- |
| 稳定角色 | soul.md、character.py | 身份、性格和说话方式 |
| 核心记忆 | core_memory.py、core_memory.json | human 为叙事画像，persona 为关系演变与相处反思 |
| 主题槽位 | memory_router.py、各域/槽位/main.md | 项目、人物、话题和自我相关事实 |
| 槽位元数据 | memory/_slots/各域.json | 标题、摘要、别名、状态及是否置顶 |
| 全局索引 | memory/index.md | 主对话前选择文件的目录 |
| 其他材料 | commitments、journal、patterns | 承诺、日记和作息等，不全走槽位路由 |

Miru 的 persona 不等于 soul，也不等于 HeyBuddy 旧 persona.md 的静态人格用途。Miru 的 human 偏叙事性画像，结构化姓名等还由 identity/profile 路径提供；不能只因名称相似就合并这些概念。

## 写入、整理与检索

`SleepAgent.enqueue()` 收集对话，当前实现以最后一次消息后 180 秒或累计 12 条触发批次。Slot Writer 根据对话和槽位索引产生新建/匹配结果；`route_with_slot_write()` 对新槽位直接保存已整理内容，对匹配槽位调用追加编辑器。

`persona_writer_state` 累积批次，通常四个批次后触发 Persona Writer，对照当前 human/persona 和新槽位摘要进行追加或替换。核心块达到阈值还可压缩。Curator 采用 Planner/Executor 做域内编辑、合并、归档；旧注释提到的自动降级期限已变为约十年，实际主要依赖手动归档。后台线程、模型调用与重试不能直接当作 HeyBuddy 的 asyncio、回执核对和显式失败重试的等价实现。

`prompt.py::call_chat_preflight()` 用索引和当前消息选择最多五个真实文件及两个补充关键词；宿主核对路径后读取片段，异常时回退关键词搜索。主模型也可调用 archival_memory_search。memory.search() 实际遍历 Markdown 并按关键词出现次数排序，不是向量检索。部分读取直接按字符截短，不是完整章节选择。

## 主模型的记忆相关工具

- archival_memory_search：query 搜索、path 读取、index 查看目录。
- add_commitment：把明确目标追加到承诺文件。
- complete_commitment：按标题模糊匹配完成项，并同步完成记录。

当前 tools 目录没有向主模型注册 Core 任意编辑工具；核心块主要由后台 Writer 更新。屏幕查看不是记忆读取，MCP 扩展也不代表所有工具都属于记忆系统。

## HeyBuddy 可借鉴什么

1. **分开稳定人格与相处记忆。** soul 定义是谁，human 保存用户资料，persona.md 现在保存有用户证据的相处认识与反思，学习器不能改写 soul。旧的非空静态人格不自动重新解释。
2. **提供主题记忆的只读搜索入口。** 生产已采用 archival_memory_search 的统一入口，搜索、路径读取、目录与索引检查复用 MemoryLookup/MemoryFS 并限制路径、文件、字节和结果，不复制无界遍历或新增知识权威数据库。原始对话 Recall 保持独立。
3. **明确身份信息的保存状态。** 姓名等陈述应有清晰目标、真实来源和保存回执。额外检索工具不能弥补写入失败；一句“记下了”不等于长期保存。
4. **保留后台整理，让前台专注交谈。** 现有 Priority/Batch、Curator 和预检索已覆盖类似分工，不必再复制另一套后台循环。
5. **承诺工具是独立产品能力。** 如需新增，必须处理重复、误匹配和写入回执，不把日常琐事全部变成任务，也不默认引入主动提醒。

当前工具与存储见 [记忆与工具说明](memory-and-tools.md)。已加入统一主题检索与关系学习，没有新增承诺或 QQ 远程写入权限。
