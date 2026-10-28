# P2 Review Fix：Recovery / Retrieval / Curation

审查起点为 0fa3e0417c327c6588afbab48753b535d3fbf42b，工作区干净；比 Review 引用的 664532a 更新，包含 P3 及其真实评测工具。三个问题仍存在：ready() 全局等待未核对回执，Preflight 替换确定性结果，Curator 只取最近两个完成 Job 的来源。

## M1：确定性恢复

实现和真实路径见 [Learning](modules/learning.md)。原 Job、Receipts、Hash 与 metrics.recovery 复用，无新 schema。启动最多核对一项，Idle 维护轮再检查一项；有 running Run 或正在学习时推迟。先记录检查次数与下次时间，保证重启不重置失败预算；只读检查 0 LLM 请求。

| 文件状态 | 恢复动作 |
| --- | --- |
| after_hash + 索引完整 | applied；全项都如此才能 completed |
| before_hash / CREATE 不存在 | not_applied；混合任务从最新 Markdown 重新规划 |
| after_hash + 索引缺失/待刷新 | 保留已保存内容，等待显式受限索引修复后核对 |
| 第三种 hash / 不可读 / 超检查预算 | 保持不确定，退避且限次；不覆盖、不回滚 |

已发布回执合入最终任务账目，重规划不能再次写同一路径；未发布部分继续走 Shared Writer 的新计划/expected_hash。来源预留记录始终指向原 Job，pending/failed 不冒充已学习完成；只在真正 completed 时累计成功更新。Curator 部分 split 复用原 pending Job，并读取原对照文件与已存在目的文件，不重复 CREATE。

本轮保留真正冲突的 Agent 级写保护。Selection/Consolidation/Curator 可能读取目标之外的文件，单纯比较不同目标路径不能证明独立；未建立依赖关系前不放宽。正常已发布、未发布和混合状态可自动恢复，真正不明状态需要用户处理。检查与安全重规划各有默认 3 次边界，退避 300 秒；最多 9 回执、2 秒协作预算，同步 IO 不保证硬取消。达到安全重规划上限的任务只有在所有文件状态可证明时才转普通 failed，不能用这种方式绕过不明副作用。

测试使用 FakeProvider/FakeClock、真实 MemoryFS/SQLite、取消和真实子进程退出。覆盖 CREATE/UPDATE、写前回执窗口、A 已发布/B 未发布、外部编辑、索引部分成功、次数持久化、前台门控/Idle 恢复及下一次学习。不会重跑工具或改原 Journal/Reflection 进度；Fake 验证不代表真实语义品质。

## 未扩展范围

历史 Backfill 是可选项，本轮不实现；没有修改 Activation Watermark，也不增加自动旧 Episode 补学。若以后加入，应显式选择有限 Episode/Run/请求额度并复用现有来源预留与 Learner，不全库扫描或隐式收费。

## M2：Preflight 保留 Baseline

ContextRetriever.preflight() 在模型调用前复制确定性结果，空选择保持原命中，未知/重复/过量 ID、Provider 失败或重新读取失败都回退到同一 Baseline。补充结果只能在有界集合的余量中加入，既有证据优先，不因新候选被挤掉；最终仍由 ContextBuilder 按原预算、Run/call_id 和 Memory 路径去重。Baseline 为空时沿原 Recall Fallback，普通接话仍不请求 Preflight。

ContextBuilder 也单独保护成功 retrieve 与可选 preflight 的异常边界，自定义 Preflight 清空副本再失败不会破坏原结果。完全重复的 Thread state 在最终 Memory 实际选入预算后才省略，避免超预算补充项使原 Thread 内容消失。tests/test_preflight_baseline.py 覆盖有/无基线、空/非法/过量/重复选择、真实 Thread/Episode/完整 Run、超预算、当前输入、自定义失败和取消；原完整工具重放、角色及快速接话回归保留。

## M3：目标相关的 Curator Evidence

所选 Markdown 路径 → 已完成 Learning Job 的 applied operation.path → 来源 ID/hash → 重新验证后的原始 Run。每路径最近四个成功修改任务，不受全局新近游戏/实习任务干扰；最多三个目标、每目标八引用、合计八 Run 和原有来源字节总量，查询复用 SQLite VM/时间预算。来源重新检查 Agent、Conversation、completed/完整性、hash 与引文。按最新相关任务选择，再按源时间输出，保留旧决定与最新更正的关系。

模型收到每个文件的来源集合与不足状态；Host 对事实修改也按对应文件集合再次验引文，不能用同批另一个文件的证据修改此文件。无来源/无法核实/字节范围不完整时保守结构整理或 no-op；事实修改明确 evidence_insufficient。关联是 Job/文件粒度，不是逐事实 Provenance Graph，语义蕴含仍需审阅。未增加 Episode/Thread fallback 或额外模型判断；不改变写入、split 或稳定偏好保护。

tests/test_curator_evidence.py 覆盖 Day1 项目证据被 Day2/3 无关写入夹在前后的情况、相关多次更新/更正、丢失/跨 Agent/变更 hash、累计字节预算、无来源 No-op 与跨所选文件误引。真实 Writer/索引/来源框架仍由既有 Curator 和集成测试验证。

## M4：集成与交付

tests/test_p2_review_integration.py 使用五天 FakeClock 场景：确认偏好与方案 A、重复偏好 no-op、明确纠正偏好与方案 B、A 对应的 Core 已保存后取消、关闭并重开真实 SQLite、启动自动核对、仅补完未发布目标、Curator 读取关联的旧决定/新更正，最后 P3 对话取得最新 Core 与 Markdown。原文件 mtime/内容证明已发布目标没有重复写；来源同一 Job 完成，原方案 A 仍在 Recall/Journal，Reflection 进度、persona.md、Character Package 不变。

最终审查补强两个直接相关边界：恢复的协作时间预算覆盖重新验证来源与刷新 Curator 对照，而不只检查 hash 循环；若时间已尽，不清空旧回执。Context 后处理只使用实际创建的 Host 引用标记，不能把历史用户伪造的 JSON 当标记解析或修改。错误的新计划试图对本任务已发布路径重复追加时明确拒绝，不以新 expected_hash 绕过幂等保护。

| 里程碑 | 提交 | 检查 |
| --- | --- | --- |
| M1 Recovery | 5d8f913 | 73 项恢复/学习/角色相关测试 |
| M2 Retrieval | 8f1519a | 42 项检索/上下文/角色相关测试 |
| M3 Curator Evidence | 944da14 | 30 项整理/来源/恢复/集成测试 |
| M4 Integration | 本报告所在提交 | 全量 702 passed、2 skipped；Ruff、格式、离线构建与 CLI 检查通过 |

最终 HEAD 见交付回复及 `git log -1 --format=%H`，避免文档自引用自身提交哈希。未新增迁移、表、事实数据库、依赖或调度平台，当前仍为 schema 12；旧迁移 1–12 与其引用 Schema 常量、P3 Character 源码/包、pytest 网络禁用保护逐项核对不变。uv.lock 不变。

本次修改集中在 learning/{service,models,curator}.py、storage/learning.py、retrieval.py、core/context.py、organization/runtime.py、config.py、.env.example 与对应模块文档。新增 tests/test_learning_recovery.py、test_preflight_baseline.py、test_curator_evidence.py、test_p2_review_integration.py；既有 Curator 与真实进程退出用例更新为目标关联证据/自动核对的行为断言。README 和 AGENTS.md 记录入口及明确授权。精准清单可用 `git diff --name-only 0fa3e04 HEAD` 查询。

## 三个不变量与限制

A：常见写中断不再要求手动解冻。持久回执在写前保存，启动/Idle 只读证明确切状态，已经发布的计入完成或作为不可重复写的对照，未发布的从当前 Memory 新规划；只有全部发布或新规划明确 no-op 才完成。第三种 hash、无法读取或索引不完整继续阻止写入；这类真正不确定状态并不承诺无人值守消解。

B：可选检索失败不破坏基线。确定性结果先深拷贝，模型空/坏选择或异常只能失去补充机会，不能清掉已验证结果；即使自定义 Preflight 修改副本再抛错，Core 也保留原检索。最终 token 预算仍可整项省略放不下的可选引用（包括基线项），当前 Run/近期历史和完整工具配对继续受原规则保护。重复 Thread 正文只在实际覆盖它的 Memory 入选后省略。

C：Curator 不再取最近无关任务作为依据。来源首先与目标路径的成功写入关联，再检查真实归属、完整性与 hash，事实修正进一步限制在该文件的关联用户引文中。它证明的是关联与引文存在，不是对语义真值的数学证明；一个多目标 Job 的来源仍是 Job 粒度，无法自动建立逐事实精确出处。缺少来源不会从旧 Markdown 推断新用户状态。

真正不确定回执仍全局保护 Memory 写入，未实现文件级依赖隔离；P1 组织、P3 被动状态和正常只读对话沿原路径运行。相关 Job 查询有 SQLite VM 预算，但同步 IO 不保证硬实时取消；极多任务可能需要未来额外派生索引，目前不修改旧迁移。只看每文件最近四个成功任务/有界 Run，缺失或范围不足时宁可拒绝事实改写。索引修复仍需显式入口，不自动扫描/改写全库。Backfill 与额外 Episode/Thread 证据 fallback 留作后续。

这里的写保护限定 Learner/Curator 的自动写入及其可信调试执行入口，不取消用户直接编辑 Markdown 或原有显式 Memory 工具授权。最后一次 `uv run pytest -q --basetemp=.heybuddy/p2fix-delivery-all` 为 702 passed、2 skipped（150.21 秒）；两项 Windows Symlink 权限跳过沿用现有环境，Junction 测试通过。uv run ruff check .、uv run ruff format --check .、uv build --offline 与 uv run personal-agent --help 实际通过。新增 31 项离线测试；未运行付费 Live 验证，不能据此声称 LLM 语义理解已改善。

纯 hash/索引恢复不发 LLM 请求；确定未发布部分的一次新规划仍使用原完整学习流水线与共享请求/token 上限，默认安全重规划最多三次，次数越界不无限付费。恢复单元占一个维护批次、只在 Idle/无 running Run 时执行，有限增加本地读取与 SQLite 检查；P3 无逐轮新增 Appraisal。Preflight 仍至多一次，Curator 仍一个 Curation 请求，关联查询不额外调用模型。本轮所有验证离线、无真实 Key/API 费用，不能据 FakeProvider 宣称语义质量收益。
