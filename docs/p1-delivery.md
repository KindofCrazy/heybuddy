# P1 初版交付、迁移与使用（历史基线 4f7a6f8）

本文保留初版 P1 的交付与当时限制；当前时间驱动、Dirty Repair、积压与取消行为以 [P1 Review Fix](p1-review-fix.md) 和模块文档为准。下文 schema 7、单批触发及缺少 Dirty 重建的限制已在 Review Fix 中修复，不代表当前 HEAD。

本轮完成 Continuous Conversation、Episode、Thread 和 Working Context 的实际接线。原 Agent Loop、Provider、ToolRegistry、Run Journal、MemoryFS、Recall 和 Reflection 继续工作。整体图和完整入口顺序见 [实际架构](architecture.md)，细节见 [Organization](modules/organization.md) 与 [Retrieval](modules/retrieval.md)。发行包版本号仍保留 0.7.2，本轮功能名为 P1，数据库 schema 为 7。

## 实现文件与职责

| 文件/类 | 关键方法与职责 |
| --- | --- |
| main.py / sessions.py | run_cli、_run_session、chat/_chat 装配持续流、异步输入和前台抢占；SessionManager.context 绑定宿主时间/检索/预算；new/resume 仅开发者兼容入口 |
| core/temporal.py | temporal_context 编译当前本地带 offset 时间与上次终结 Run 开始时间的间隔，未记入 Journal |
| organization/models.py | Episode/Highlight/EvidenceRef/Source/Batch、Thread/ThreadDecision 与有界可信 OrganizationPolicy，均为严格 Pydantic 模型 |
| organization/provider.py | OrganizerModel.ask，空工具、独立输入/输出 tokens/可见字节/超时及 usage 校验，每次至多一次请求 |
| organization/episodes.py | EpisodeOrganizer.organize、gap_kind、validate、evidence；时间候选、语义建议、顺序/完整覆盖/精确引文校验 |
| storage/episodes.py | EpisodeRepository.batch/sources/publish/finalize/recover；来源 hash、有限尾部修订、版本、原子进度和明确 skip |
| organization/threads.py / storage/threads.py | ThreadOrganizer.organize；snapshot/publish 独立版本 CAS、单一关联、状态刷新与无 Thread 分支 |
| organization/runtime.py | AsyncInput 和 OrganizationRuntime.schedule/foreground/command/close；只在进程存活期间有界组织，所有 SQLite 在事件循环线程 |
| retrieval.py / core/retrieval.py | ContextRetriever.retrieve/keywords、ContextRetrieval Protocol、RetrievalPolicy/Result/Item；fast、Thread/Episode/原始 Run、Recall fallback |
| core/context.py | _retrieve、_select_history、for_request、prepare；每 Run 缓存检索，逐 round 按最新工具/当前 Run 预算重选、去重和完整重放 |
| core/compaction.py | SUMMARY_PROMPT 限定为仍影响当前交流的工作摘要，检查点事务不变 |
| config.py / .env.example | organization_policy、retrieval_policy 与 AGENT_ORGANIZATION_/AGENT_RETRIEVAL_ 配置，不让模型改变权限/预算 |
| storage/sqlite.py | 追加 migration 6/7 与 last_interaction，不改已发布 1–5 |
| evaluation/p1.py | 独立 --live 合成 smoke、固定预期与预算，summary 无正文，私人材料只在忽略目录，无 Apply |
| tests/test_continuous.py / test_episodes.py / test_threads.py | 持续流、时序/语义契约、来源、修订、事务、迁移与归属 |
| tests/test_organization_runtime.py / test_working_context.py | 触发/抢占、实际输入桥、上下文/预算/去重/完整工具与 Provider 数据、Recall 与权限边界 |
| tests/test_p1_recovery.py / test_p1_smoke.py | 真正子进程提交前后退出、终端等待退出、Memory 取消/显式 retry、独立评测安全入口 |

建议保留 SessionManager 名称，因为其已有选择/归属/checkpoint 校验可靠；本轮只降低其用户可见程度。Core 使用独立 Retrieval Protocol，具体 SQLite 检索放在宿主服务，维持依赖方向。topic_threads 在 migration 6 先建空表以提供真实 Episode 外键，Thread 行为/版本/进度由 migration 7 接通。

## 数据库迁移与兼容

启动时先取得同数据库单实例占用锁，再 initialize()，整个顺序迁移和 user_version 原子提交。新表如下：

| 版本 | 新对象 | 关系/不变量 |
| --- | --- | --- |
| 6 | episodes | stream_id 属于现有 Conversation，最多一个 provisional 尾部，最多一个 thread_id |
| 6 | episode_runs | run_id 唯一，Run/stream 与 Episode/stream 复合外键，ordinal 保持原始顺序 |
| 6 | episode_versions | Episode ID + revision 主键，仅派生摘要/Run 引用，不复制聊天原文 |
| 6 | episode_organizer_progress / episode_skips | 独立 cursor/revision/状态/安全错误/计量；失败、取消、中断、oversized 明确记录 |
| 6 | topic_threads | Agent 归属、当前 state/revision/时间/dirty；Episode Thread 触发器复核同 Agent |
| 7 | thread_organizer_state | Agent 范围独立批次进度与错误，不占 Reflection/Compaction 边界 |
| 7 | thread_versions / thread_batches | 状态版本、所据 Episode 修订、关联决策和旧 Thread 版本来源，均有存储大小上限 |

旧 client_state 存在时沿用原来选中的 Conversation；没有选择才新建持续流。其他旧流不迁移归属、不静默合并，仍可 Recall。旧 agents.system_prompt 仍仅兼容元数据，Base 来自代码。旧 messages、runs、context_summaries、reflection_plans/progress、automation_batches 原位保留，不删除、不重建。新组织器初始从当前流未整理来源开始分批处理；其他旧流可用开发者 /resume 后显式组织。

completed 是唯一 Episode 可整理终态；其中工具失败也只能描述原实际结果。failed/cancelled/interrupted 作为不可跨越的屏障，跳过并持久记原因；running 不提前推进。来源比字节/数量上限更大时记 oversized 并前进，不假装已提炼；完整 Journal/Recall 保留。模型输出、来源变化、CAS 或事务失败不推进；手动 organize 明确 retry，自动失败不无限重复调用。Organizer 的 running 尝试重启恢复 pending，因为提交前只有纯建议，提交后 cursor/关联已原子完成。

## 使用与可演示场景

```powershell
uv sync --locked
uv run personal-agent --help
uv run personal-agent
```

保持既有 .env，无需覆盖。必需的 Key/模型/窗口仍来自环境或被忽略的本地 .env，不在命令行粘贴凭证。常用交互：

```text
直接聊天，不需要 /new 或 /resume
/status
/context status
/episode organize
/episode status
/episode list
/episode show <id>
/thread organize
/thread status
/thread list
/thread show <id>
继续昨天的 HeyBuddy 架构讨论
/memory automation status
/memory automation retry
```

场景 A：紧邻交流只使用已有完整 Runs，fast path 无额外检索模型。B：手动 organize 一批立即生成带时间/版本/来源 Episode，或等默认空闲 25 分钟/累计 8 条。C：相隔超过 3 小时的同话题保持不同 Episode，通过 Thread 联系；一次性闲聊可无 Thread。D：明确恢复旧话题时查询 Thread、最近默认三个 Episode 与至多两个完整 Raw Runs，省略重复/不合预算的可选历史。E：检索遗漏或摘要不足时主 Agent 仍可 Recall。F：模型/数据库/进程失败不改变已经 completed 的普通 Run，重复整理不重复归档。

episode status/list/show 与 thread 命令不是模型工具，不创建普通 Run、不授予 Memory 权限。status 的计量不含正文；list/show/review 是用户显式查看私人材料。

## 独立真实模型 smoke 方案

```powershell
# 不读密钥、不请求模型
uv run python -m personal_agent.evaluation.p1 --help
# 显式付费入口：只使用全新合成数据，最多六次请求和 196608 预留 tokens
uv run python -m personal_agent.evaluation.p1 --live --max-requests 6 --max-tokens 196608
```

该命令使用当前配置的 Provider，不访问现有真实会话，输出至 `.heybuddy/evaluations/p1-<随机ID>/`。固定四条历史覆盖短时间换话题、本地午夜和跨天；增加合成近期聊天及固定检查点后验证旧话题恢复。summary.json 是无正文检查/计量，private-review.json、数据库和 Markdown 只在忽略目录，供人工逐项核对：分段是否合理；用户决定是否被误读为建议或反过来；晚饭是否错误归入项目；最新更正是否出现在 Thread state/回答；Recall 是否使用真实证据；没有 Memory Apply。

本轮未执行 --live，真实语义分段、Thread 匹配和回答质量未验证。离线 FakeProvider 仅验证宿主契约。固定预期/原评测 scorer 不随输出改动；P1 Summary 的新职责快照独立保存，旧 Phase 1/retained/Holdout/retention 保留，不宣称 Prompt 收益。Stage 计量分开统计，reasoning 不重复加到 output，未知 usage 保留 unknown。

## 限制、技术债与 P2 接口

- 阈值是启发式，15/90/180 分钟不代表真实心理/话题边界；跨午夜无特殊行为。当前只修订一个近期 provisional 尾部、最多配置批次容量，不能全历史任意重分段或合并已 finalized Episode。
- 引文匹配、ID/时间/版本校验证明结构和证据存在，不能证明 LLM 的决定/假设/建议语义判断正确。Thread 当前状态同样是派生理解。语义质量需要上面的 live + 人工复核。
- 标题关键词检索可能漏同义词、过宽匹配或只选到一个话题；没有额外 LLM Planner、向量或消歧。Episode fallback 默认只看最近 200 条，深历史仍靠 Recall。默认检索数量/字节/tokens 可用 AGENT_RETRIEVAL_* 有界调整。
- 一条完整 Run 或批次 JSON 无法放入组织输入预算时停止并保留诊断，需显式缩小 PAGE_RUNS 或调整可信配置；不截断成成功。正常上下文的不可缩减 Run 继续明确失败；可选旧 Raw Run 则整条省略。
- 只有 CLI 进程存活时运行 idle hook。持久 cursor/dirty 支持重启继续，但没有系统服务、调度平台或无界 backlog 消费；每次一个 Episode 批次、至多一次 Thread 请求，剩余积压由后续触发/手动命令处理。原 MemoryAutomation 最多四次阶段请求另计；检查被新输入取消后按原规则显式 retry。
- SQLite/Markdown 短步骤仍同步，VM/asyncio 超时不能硬取消阻塞磁盘。终端 daemon 只读输入，没有数据库跨线程访问。操作系统当前本地 offset 是展示策略，不提供跨历史 DST 的 IANA 时区重建。
- Episode cursor 基于应用追加的 runs.rowid；应用不删除/重排 Journal。直接在外部改写 Run、重排 rowid 或维护数据库的恢复工具尚未提供；不能把来源 hash/CAS 当作对恶意本地修改的认证。普通退出、事务失败和重启路径已有测试。
- Episode 重分段/移出旧 owner 会标记旧 Thread dirty；没有全量状态重建，检索省略其旧 state，后续相关 Episode 或手动整理刷新。只改 finalized 状态而摘要未变时不额外作废当前 Thread state。
- **现有 Worthiness 成功阴性判断仍推进 reflection_progress**，表示已筛查，不表示已提取/批准/写入。P2 需要区分筛查、学习、审批与应用边界，并制定重新考虑历史的策略；本轮没有顺手重构这部分。

| P2 可复用接口 | 需要后续扩展 |
| --- | --- |
| EpisodeRepository.get/list/sources、Episode/Highlight/EvidenceRef、版本/Run 引用 | 跨多页、按 Agent 的学习来源窗口及独立学习进度；语义确认/冲突策略 |
| ThreadRepository.get/list/snapshot、Thread revision/current_state/memory_ref | 可信 MemoryRef 生命周期、语义 Memory 归属和失效/重建规则；不能让模型直接写任意关联 |
| ContextRetrieval Protocol、RetrievalResult/Policy、ContextBuilder.prepare | 多话题消歧、可选有界语义决策、长期知识检索与来源优先级，继续由 Builder 控制预算 |
| MemoryFS 的受限路径/expected_hash/原子发布/index | 将整理理解转换为待审长期知识；继续保留唯一 Markdown 权威源与跨存储不确定窗口 |
| ReflectionService 的来源验证、计划、可信审批、Apply/reconcile | 接入 Episode 证据时必须展开并验证原始 Run，不把派生摘要当作用户原话；独立 P2 进度 |
| Provider Protocol、OrganizerModel、Budget/usage 与低信任边界 | 真实模型配对评测、质量/成本校准；不能用小样本或词面命中声称收益 |

## 验证记录

各里程碑均实际跑离线测试、Ruff、格式、构建和帮助入口后提交。M1：433 passed/1 skipped；M2 完整回归 463 passed/1 skipped，随后补充组织测试 35 passed；M3：483 passed/1 skipped；M4：494 passed/1 skipped。最终命令 `uv run pytest --basetemp=.heybuddy/p1-final-verified`：**505 passed、1 skipped，94.99 秒**。跳过为本机 Windows Symlink 权限限制；没有剩余失败。P1 核心与旧回归保持 tests/conftest.py 网络禁用保护，真实 smoke 没有执行。

实际执行并通过：`uv run ruff check .`、`uv run ruff format --check .`、`uv build --offline`、`uv run personal-agent --help`、`uv run python -m personal_agent.evaluation.p1 --help`。构建得到 wheel/sdist。最终增补验证了 ToolResult 引文必须匹配实际有界投影、sequence 不连续不能伪装精确来源；真进程在提交前/后退出、等待真实 stdin 时退出均离线验证。

Windows 沙箱内 asyncio 本地 socketpair 初始化会挂起，因此异步测试在批准后的沙箱外运行，仍受 pytest 离线保护；uv cache 与 pytest 临时目录置于被忽略的 .heybuddy 下。构建首次联网获取声明的 hatchling，后续 uv build --offline 成功，不涉及真实模型。没有读取真实 .env 内容或把私人数据库当测试样本。
