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

## 后续里程碑

M2 修复 Preflight 非破坏合并；M3 修复目标相关证据；M4 进行跨 P1/P2/P3 集成与完整回归。各自完成后更新本报告并分别提交。历史 Backfill 为可选项，本轮暂不扩展默认来源水位或历史学习入口。

## M2：Preflight 保留 Baseline

ContextRetriever.preflight() 在模型调用前复制确定性结果，空选择保持原命中，未知/重复/过量 ID、Provider 失败或重新读取失败都回退到同一 Baseline。补充结果只能在有界集合的余量中加入，既有证据优先，不因新候选被挤掉；最终仍由 ContextBuilder 按原预算、Run/call_id 和 Memory 路径去重。Baseline 为空时沿原 Recall Fallback，普通接话仍不请求 Preflight。

ContextBuilder 也单独保护成功 retrieve 与可选 preflight 的异常边界，自定义 Preflight 清空副本再失败不会破坏原结果。完全重复的 Thread state 在最终 Memory 实际选入预算后才省略，避免超预算补充项使原 Thread 内容消失。tests/test_preflight_baseline.py 覆盖有/无基线、空/非法/过量/重复选择、真实 Thread/Episode/完整 Run、超预算、当前输入、自定义失败和取消；原完整工具重放、角色及快速接话回归保留。
