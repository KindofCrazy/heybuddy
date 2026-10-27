# V0.7.2 Phase 1 检查点

本阶段在 V0.7.1 之上完成回答后 Worthiness 与原 Reflection 的自动衔接，没有修改已有 Main/Reflection/Compaction Prompt 或工具描述。新增 Worthiness 专用 Prompt，作为后续 Phase 2 的真实基线组成。

默认 suggest：LLM 负责长期价值与内容整合，宿主控制 completed 源、调用/Token/时限、持久状态和批准。仅在可信配置给出精确 knowledge/ 路径及完整 Markdown SHA256 时，auto 才可发布小型 confirmed project/knowledge 新文件；不自动写任意新内容、Core 或纠正。范围与限制见[Automation 模块](modules/automation.md)。

schema 5 增加 automation_batches，独立保留 Worthiness 决策、进入反思、计划链接和额外计量。成功阴性判断与审阅边界原子提交；这表示成功筛查而非保存知识。阳性待审不推进写入/审批状态。失败阻止自动重试；显式 retry 复用已成功的判断。已有 pending 计划时暂停并提示积压。

实际验证：完整离线 pytest 422 passed、1 skipped（Windows Symlink 权限），Ruff check/format、uv build、CLI help 通过。wheel 的三个独立 CLI 进程完成普通问答后负判断、稳定偏好后自动提案、重启后拒绝：额外模型请求分别为 1、4、0；审阅边界为 2、2、4；Markdown 未改变。自动化测试均 Fake/MockTransport，原网络禁用保护不变。

这证明机制/审批/恢复契约，不代表 LLM 语义质量；下一阶段将冻结当前 Prompt 和 Ground Truth，使用同一模型/配置做 Baseline/Candidate 的独立真实评测。没有后台 Runtime、Scheduler 或 ToolCallReviewer。
