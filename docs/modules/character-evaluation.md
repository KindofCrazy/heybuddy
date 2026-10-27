# Character Evaluation

本轮真实 API 结果见 [P3 live 检查](../p3-live-validation.md)。evaluate() 参数增加输出总 tokens 和请求超时，条件指纹包含二者；CLI --output-tokens 可显式控制包含 reasoning 的额度，默认 512 保留旧配置。真实对比建议 `--output-tokens 2048 --max-tokens 2200000`。Trace 在异常时也统计安全 usage，不保存未完成正文；日志标准化消息排除 provider_data，真实 Provider 仍收到原样的完整重放数据。

`evaluation/character_state.py` 提供独立 --live smoke：main() 在载入配置前检查显式授权；run() 新建私有合成目录/SQLite/Memory，Clock 固定时间，Host 标记两条 completed Run，顺序调用 EpisodeOrganizer/StateService。最多 4 请求/200000 预留 tokens，Episode 输出 4096、State 输出 2048。第二个事件关闭话题，然后验证去重、有效期、无 Memory 写入与空工具；结果存本地 private-review.json，异常仅打印安全类型，失败检查返回非零。不调用 Main Agent、无 Apply 或主动聊天。该入口测模型更新与发布，Idle/抢占由原离线测试覆盖。最小执行为 `uv run python -m personal_agent.evaluation.character_state --live`，--help 不加载凭证；tests/test_character_evaluation.py 验证授权守卫。

## 职责与组成

`evaluation/character_cases.py` 冻结 13 个合成情境、20 轮漂移和七个独立评分维度。`evaluation/character.py` 复用 evaluation.runner.Budget、真实 Agent/ContextBuilder/Provider Protocol/ToolRegistry，提供离线默认与显式 --live 的 E0/E1/E2 对比。没有额外 Judge、第二个角色 Agent 或写记忆工具。

## 实际运行路径

1. `uv run python -m personal_agent.evaluation.character` 进入 main()，解析变体、case IDs、请求/token 额度和输出目录，验证目录在源码库内时被完整忽略，再创建唯一 p3-* 子目录。默认构造 OfflineProvider，不加载 Settings/.env/Key，不创建真实 API Client。
2. evaluate() 加载同一个 sherlock_bbc 包。每个场景每一轮重建 InMemoryHistory 和 FixedRetrieval，固定时间、工具、模型/窗口/输出参数并计算 condition_hash。固定检索是合成 P1/P2 输出夹具，不重新生成 Episode/Thread，不代表组织模型质量。
3. E0 只有 Profile；E1 增加 Behavior 和 Examples；E2 再接入受控状态夹具。漂移样本前七轮 curious、随后 focused，第十四轮起无有效状态；状态更新由运行时测试验证，评测不增加维护请求。Lore/历史/检索规则相同。
4. Agent.run() 正常生成，Trace 在每次请求前 Budget.reserve()，记录合成完整请求、答案、工具调用、Provider usage 和耗时。工具案例执行真实 add_numbers，Registry 校验和 call_id 配对仍执行；没有 Memory Apply 或外部副作用工具。
5. 下一轮使用相同合成 teacher-forced 前文，不使用候选答案，确保三组暴露条件完全相同。condition_hash 可逐轮配对。该模式测固定历史下的漂移敏感性，不代表自由生成长对话的自反馈漂移。
6. 每组输出独立 E*.json，包含机械 checks、请求/估算/usage/延迟、待人工评分 rubric 和合成请求。usage 是本组增量，shared_budget 是整个运行累计。CLI stdout 只给目录、检查和总额度，不打印正文/推理。

## 评分、异常与边界

六个语义维度分别按 0–3 人工标注，Cost & Latency 直接读数，不合并为总分。FakeProvider 只验证接线、工具和变量控制；所有语义 score 保持 null，必须以真实输出人工审阅后填写，禁止把关键词出现或契约通过当作人格质量分。建议盲化变体，按 case/turn/condition_hash 配对，比较漂移第 1/10/20 轮、纠错、挫折、跨 Episode/Thread 和 Canon 边界。冻结 corpus_hash/package_hash，不根据候选得分改题。

E3 不接受。真实生成需要显式 --live，同一可信配置模型/端点且 max_retries=0。额度不足停止，不扩容/重试；当前未完成组不写完成报告，已保存组保留。adapter 安全处理未完成响应，不保存 reasoning；配置/API 错误只显示类型，不能视为语义低分。状态维护开销需另看 Runtime 的 character_state 计量，夹具评测不能测得真实状态更新质量或成本。

## 阅读与验证

```powershell
uv run python -m personal_agent.evaluation.character
uv run python -m personal_agent.evaluation.character --variant E2 --cases research,own_error,tool_review
# 以下会付费，本次没有执行：
uv run python -m personal_agent.evaluation.character --live --variant all --max-requests 120 --max-tokens 2000000
```

默认输出 `.heybuddy/evaluations/p3-*`，不提交正文。tests/test_character_evaluation.py 覆盖三组同条件、20 轮、层级消融、状态失效、真实工具配对、语义分留空、预算停止和默认不加载凭证。真实 P1/P2 组织/检索回归见 tests/test_p2_integration.py、tests/test_threads.py 与 tests/test_memory_retrieval.py。
