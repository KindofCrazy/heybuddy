# HeyBuddy 开发文档

当前文档描述 V0.7 的实际实现。建议先看整体 Pipeline，再沿调用路径进入具体模块。

## 阅读顺序

1. [整体架构与 Pipeline](architecture.md)：对象关系、模块依赖、启动、请求执行、切换、恢复及事务边界。
2. [Context Budget 与 Compaction](modules/compaction.md)：完整 Run 边界、增量摘要、原子检查点、有界加载和失败恢复。
3. [Agent Loop](modules/agent.md) 与 [ContextBuilder](modules/context.md)：一次输入如何变成模型上下文、工具循环和最终结果。
4. [Provider](modules/provider.md) 与 [工具注册](modules/tools.md)：模型接口转换、工具参数校验及超时契约。
5. [SQLite 存储](modules/storage.md) 与 [数据模型](modules/models.md)：消息怎样保存、排序、关联和恢复。
6. [MemoryFS](modules/memory.md)：Core/Deferred 分层、索引发现、摘要冲突、原子文件、大小和权限边界。另见[Letta Code 调研](memory-design.md)与[迁移说明](memory-migration.md)。
7. [Conversation Recall](modules/recall.md)：FTS5 中文取舍、schema 迁移、派生索引、只读证据与重建。
8. [会话管理与 CLI](modules/sessions-cli.md) 与 [配置](modules/config.md)：应用入口、会话选择和配置校验。

[Reflection 学习流程](modules/reflection.md)：候选证据、已有记忆整合、人类审批、部分写入与跨进程恢复。

## 按问题查阅

| 想弄清楚的问题 | 文档入口 |
| --- | --- |
| 程序启动时先做什么，何时创建 SDK 和 Runtime？ | 整体架构的启动 Pipeline |
| 一次用户输入为何可能触发多次模型请求？ | 整体架构的请求 Pipeline、Agent Loop |
| 为什么 Agent 核心不需要认识 SQLite？ | 整体架构的依赖方向、ContextBuilder |
| 工具何时执行，执行前后分别保存什么？ | 整体架构的时序图、事务边界、SQLite 存储 |
| 工具超时是否代表没有副作用？ | 工具注册与超时契约、整体架构的失败路径 |
| 为什么有些数据库记录不会出现在模型上下文？ | 整体架构的四种历史视图、Run 状态转换 |
| 重启后 reasoning 和工具调用如何重新发送？ | Provider、整体架构的重启 Pipeline |
| 记忆何时刷新、怎样授权和检测冲突？ | Core Memory、整体架构第 11～13 节 |
| 新会话如何查以前的决策，索引坏了如何修复？ | Conversation Recall、整体架构第 16 节 |
| `/new`、`/resume` 会替换哪些对象？ | 会话管理与 CLI、整体架构的会话切换 Pipeline |

源码位置均相对于项目根目录，使用实际类名、函数名定位。图中的调用顺序对应当前代码，不是未来功能的设计草图；测试入口见各模块文档及整体架构末尾的验证映射。

[V0.7.1 Reflection 真实验证](reflection-live-validation.md)：真实模型额度诊断、提案质量对照及实测边界。

[Memory Automation](modules/automation.md)：成功回答后的 Worthiness、原 Reflection、可信写入范围、持久状态与额外成本。
