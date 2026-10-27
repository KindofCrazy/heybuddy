# 自动化行为评测

## P1 独立 smoke

`evaluation/p1.py` 提供 `uv run python -m personal_agent.evaluation.p1 --live --max-requests 6 --max-tokens 196608`。没有 --live 时在加载配置前拒绝；--help 不读密钥。evaluate() 先建全新受隐私检查的合成目录，只生成固定四条带时间的聊天、普通 Markdown 和 SQLite；顺序调用 Episode/Thread、用合成 Working Summary 检查点模拟旧历史退出近期窗口，再运行真实 Agent/检索/Recall。无 Memory Apply、无自动 Memory 检查，不访问真实数据库。

SmokeProvider 在请求前复用 Budget.reserve()，记录 episode/thread/main/compaction 阶段与独立 usage，reasoning 仅是 output 的子集。summary.json 不含正文，private-review.json、原始数据库/Memory 只存忽略目录供人工复核。固定预期检查短时间话题分段、跨本地午夜、跨天 Thread、闲聊无 Thread、检索、Journal/Memory 不变；不根据模型得分改预期。corpus_hash/prompt_hashes 用于复跑比较。

该入口当前仅通过 FakeProvider 离线验证（tests/test_p1_smoke.py），没有执行真实模型；contract_pass 不能证明语义质量，报告始终带 semantic_review_required。完整复核清单与限制见 [P1 交付](../p1-delivery.md)。原 16 Case、Holdout、scorer、baseline/retained 快照不改。

## 职责与架构组成

`evaluation/cases.py` 保存 16 个预先定义的合成场景、初始历史/记忆、用户输入和期望行为，覆盖 12 类任务，12 个开发 Case、4 个 Holdout。`scoring.py::score()` 固定程序 Rubric；权限、文件不变、历史完整、工具/路径、Action/来源等可机械检查，正文 marker 只作为有限语义代理。`prompts.py` 保存/安装评测专用快照；不改宿主权限或工具实现。`runner.py` 用真实 Agent、SQLiteStore、MemoryFS、Recall、ReflectionService 和 MemoryAutomation 执行。`__main__.py` 是显式付费入口，与正常 CLI 和 pytest 分开。`reporting.py` 只读取已收集的合成结果，重新校验评分并导出无正文的汇总。

## 代码实际运行路径

1. `python -m personal_agent.evaluation --list-cases` 仅列出清单；`--snapshot <新路径>` 调用 `prompts.capture()` 在隔离临时 SQLite 上注册工具，保存实际 Prompt/description，拒绝覆盖已有基线。这两个入口不加载真实密钥、不调用 API。
2. 只有 `--live` 才 `load_settings()`，允许显式覆盖 model/endpoint/context-window；快照来自 `--prompt-file` 或当前源码。价格和调用/token/cost 上限由参数提供，非法预算拒绝。SDK max_retries=0。
3. `live()` 在 `.heybuddy/evaluations/run-<variant>-<随机ID>` 创建全新目录并检查 Git 隐私。每个 Case/重复独立 `evaluate_case()`，最多两个并发 Case，同一 Case 的 Agent 工具仍按原顺序执行。
4. `evaluate_case()` 初始化真实临时数据库和 Agent Memory Root，seed 合成历史和主题文件。长上下文场景 seed 16 个完整 Run；跨会话场景新建 Conversation，并用旧会话标记检查上下文泄漏。其他场景默认无 Memory 写权限，Automation 始终 suggest，不执行 Apply。
5. `TraceProvider.generate()` 在调用前由 `Budget.reserve()` 保守预留输入/输出，超出 request/token/cost 任一上限就停止。实际返回后记录阶段、usage、耗时、正常文本和工具调用；不导出原始 reasoning。模型响应仍经正常 Journal 保存完整 Provider JSON，仅位于忽略的合成数据库中。
6. 正常答案后复用 MemoryAutomation；生成计划仅观察，不批准。检索走真实 Registry/Recall/MemoryFS；失败工具是固定 demo_status。比较执行前后 Markdown 和历史，收集计划/来源校验结果，`score()` 计算固定检查。`result.json` 与逐 Case 更新的 `report.json` 保存私有原始轨迹。
7. `reporting.load_run()` 校验完整集合、无重复、相同模型/参数/语料/Prompt 哈希，重算固定评分；`campaign()` 仅将相同 Case/重复配对。普通请求、Compaction、Memory Automation 分开统计，reasoning 是 output 子集，不重复计费。结果导出 `evals/results/summary.json`，没有回复正文、引文或密钥。

## 运行与复现

先配置本地 `.env`，不要在命令行粘贴 Key。以下真实调用会收费；snapshot/list/reporting 不收费。若更换模型或参数，重跑对应基线，不能直接与这次结果混合。

```powershell
uv run python -m personal_agent.evaluation --list-cases
uv run python -m personal_agent.evaluation --snapshot .heybuddy/current-prompts.json
uv run python -m personal_agent.evaluation --live --variant baseline-dev --split dev --prompt-file evals/prompts/phase1-baseline.json --repetitions 2 --max-requests 128 --max-tokens 2000000 --input-price 0.3 --output-price 1.2 --max-cost 1.5
uv run python -m personal_agent.evaluation --live --variant retained-holdout --split holdout --prompt-file evals/prompts/retained-candidate.json --repetitions 2 --max-requests 40 --max-tokens 800000 --input-price 0.3 --output-price 1.2 --max-cost 0.8
uv run python -m personal_agent.evaluation.reporting --output evals/results/summary.json
```

上例显式限制 baseline-dev 为 128 requests/2M reserved tokens/$1.5；总实验额度见 manifest 与 [A/B 结果](../prompt-ab-results.md)。命令中价格是本次峰值 cache-miss 估算，复现前按真实服务商校对。`--model`、`--endpoint`、`--context-window` 可显式设置；`--cases` 逗号列表支持分组，`--concurrency 1` 可串行。重复 variant 时 reporting 会拒绝混合多次实验；通过 `--output` 为新 campaign 选独立的忽略目录，reporting 的 `--root` 选相同目录。

## 异常与边界

预算失败不自动扩大/重试；失败普通 Run、失败 Automation 和未完成采集分开记录。未知 usage 标为不完整，费用只代表已知部分，不是账单。隐私检查在目录创建后、数据写入前执行；自定义输出必须在仓库外或被完整忽略。不要提交原始 result.json、SQLite 或 Memory。汇总仅有计数、时长、安全错误/检查名与哈希。

manifest 保存 Phase 1 checkpoint、冻结语料与收集器哈希、基线哈希、预算和价格。收集结束后仅纠正了 automation_requests 的报告分类：Compaction 不再误算为自动记忆；原始 stage、运行逻辑与评分不变。manifest 同时记录前后收集器哈希，汇总从原始 stage 重算。

小样本和单模型不能证明普适提升；词面命中不能证明完整语义质量。本轮没有 LLM Judge，不用模型自评证明权限或事实正确；偏好扩写与 Holdout invalid_create 经过人工复核，并保留失败。开发分组不是最终组合在全部开发集的复测，不能合成一个“最终总准确率”。Holdout 不用于再次调 Prompt。

## 阅读与验证

`tests/test_evaluation.py` 离线验证真实 Runtime/Automation 的 Fake 路径、检索工具、预算调用前拒绝、不可被好答案覆盖的安全失败、快照一致、Compaction 计量分离和不完整采集拒绝。默认 pytest 保持网络禁用，真实评测仅通过单独显式命令运行。
