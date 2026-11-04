# Agent 内部架构文档

先顺序阅读[一条消息在 Agent 内部怎样完成](architecture.md)，再进入专题。主线从 ChatService.run 收到普通文本开始，到 RunResult 返回结束，适配层作为外围参考。

当前文档基线是 P5、SQLite schema 23。模块说明用实际源码路径和符号定位；“未来改进”是待讨论建议，不改变当前能力和授权。少数保留的版本章节用于解释兼容路径，旧交付结果统一见[历史归档](archive/README.md)。

## 主请求与执行

| 模块 | 内容 |
| --- | --- |
| [runtime](modules/runtime.md) | 应用装配和前后台交接 |
| [agent](modules/agent.md) | 模型循环、响应与停止 |
| [harness](modules/harness.md) | 来源、共享预算、扩展装配 |
| [context](modules/context.md) | 输入编译与有界历史 |
| [compaction](modules/compaction.md) | 工具产物、micro、历史和 Run 内摘要 |
| [models](modules/models.md) | 内部消息、调用、结果 |
| [provider](modules/provider.md) | Provider Protocol 与 SDK 转换 |
| [tools](modules/tools.md) | 注册、权限、参数和执行 |
| [hooks](modules/hooks.md) | 生命周期与执行前拒绝 |
| [tracing](modules/tracing.md) | 请求审计与轮次 |

## 能力扩展

| 模块 | 内容 |
| --- | --- |
| [skills](modules/skills.md) | 只读操作知识 |
| [skills-mcp](modules/skills-mcp.md) | MCP 连接、发现与白名单工具 |
| [planning](modules/planning.md) | Todo、Task、Goal 与验收 |
| [execution](modules/execution.md) | 工作文件、命令、子执行与后台 |
| [workflows](modules/workflows.md) | 固定流程和显式恢复 |

## 记忆、检索与维护

| 模块 | 内容 |
| --- | --- |
| [memory](modules/memory.md) | Markdown Core/Deferred 与写入 |
| [recall](modules/recall.md) | 原始聊天证据 |
| [retrieval](modules/retrieval.md) | Working Context 与可选 Preflight |
| [reflection](modules/reflection.md) | 证据、计划、人工审批 |
| [learning](modules/learning.md) | Host 自动学习与回执核对 |
| [curator](modules/curator.md) | 低频整理与证据 |
| [automation](modules/automation.md) | P2 关闭时的兼容模式 |
| [organization](modules/organization.md) | Episode/Thread 事后组织 |
| [organization-schedule](modules/organization-schedule.md) | 维护预算、时间与抢占 |
| [thread-repair](modules/thread-repair.md) | 派生话题修复 |
| [evidence](modules/evidence.md) | 来源投影与精确引文 |

## 主动关注与明确提醒

| 模块 | 内容 |
| --- | --- |
| [proactive](modules/proactive.md) | Shadow 候选、Wake、Attention 与覆盖 |
| [outbox](modules/outbox.md) | 草稿、Fake Sender 与接续 |
| [scheduler](modules/scheduler.md) | 明确一次性提醒队列 |
| [calendar](modules/calendar.md) | 有限时间语法与时区 |

## 角色与输入

| 模块 | 内容 |
| --- | --- |
| [persona](modules/persona.md) | 当前 persona.json 编译 |
| [prompts](modules/prompts.md) | 程序规则和交谈提示词 |
| [character](modules/character.md) | 旧 Markdown 角色兼容 |
| [character-state](modules/character-state.md) | 兼容角色短期状态 |
| [images](modules/images.md) | 图片引用与有界读取 |

## 持久化、入口与验证

| 模块 | 内容 |
| --- | --- |
| [storage](modules/storage.md) | SQLite 事务、迁移和恢复 |
| [sessions-cli](modules/sessions-cli.md) | 会话选择和命令 |
| [config](modules/config.md) | 配置加载与校验 |
| [dashboard](modules/dashboard.md) | 只读 Observatory |
| [evaluation](modules/evaluation.md) | 机制与行为评测 |
| [character-evaluation](modules/character-evaluation.md) | 角色离线评测 |
| [qq](modules/qq.md) | 官方 QQ 适配 |
| [qq-turns](modules/qq-turns.md) | QQ 收件与轮次 |
| [napcat](modules/napcat.md) | 本机 NapCat 适配 |

## 外围操作资料

[Observatory 使用](observatory.md)、[CLI 实践](cli-dogfooding.md)、[旧 Memory 迁移](memory-migration.md)、[NapCat 配置](napcat-setup.md)、[QQ 配置](qq-setup.md)、[明确提醒投递](reminder-delivery.md)。[架构审查](reviews/architecture-audit.md)保留为审查记录，不代替当前调用路径。

## 文档清理规则

当前实现只在主线和模块文档维护。阶段交付、真实调用验证和第三方方案对照放 archive，并标注历史性质。删除了已失效的“当前开发角色 Miru”和重复的“当前记忆系统与模型工具”；对应内容由 persona、memory、tools 接管。没有清理生产数据、测试 fixture、角色来源声明或用户未提交代码。
