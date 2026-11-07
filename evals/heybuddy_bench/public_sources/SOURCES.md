# 公开来源、署名与复现

公开题均为有修改的 HeyBuddy 决策任务。保留原生标签不表示继承原题的评测含义。`selected_source_records.jsonl` 保存每个样本的 `source_id`、固定版本原文 URL、原始文件 SHA-256、原标签及原文摘录；`gold.jsonl.provenance.adaptation` 解释任务变化。模型只读取单独的 `inputs.jsonl.input`。

| 来源 | 固定 revision | 实际采用 |
|---|---|---|
| [TIAGE，Huiyuan Xie 等](https://github.com/HuiyuanXie/tiage) | `08f98dfa066a5f2fba2bbd1d6ed6ee9a783a2b1d` | test 对话 1 的 turn 3/4、对话 2 的 turn 4/5、对话 3 的 turn 5；索引从零开始 |
| [IRC Disentanglement，Jonathan K. Kummerfeld 等](https://github.com/jkkummerfeld/irc-disentanglement) | `82ed04f9627a45d0d6f2ec3c8683c88eb408a0d5` | `2005-07-06_14` 的 message 1001/1002/1005；保留对应 parent links |
| [LongMemEval，Xiaowu 等](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned) | `98d7416c24c778c2fee6e6f3006e7a073259d48f` | 8 个 knowledge-update ID，oracle 用户证据裁剪；不是 S 全历史 QA |
| [MemoryAgentBench，HUST-AI-HYZ](https://huggingface.co/datasets/ai-hyz/MemoryAgentBench) | `7ea066982b140a19337e17e60d45d4076e042faf` | Conflict_Resolution row 4，`factconsolidation_sh_6k_no0` 至 `no3`；四个不同关系 |
| [TriggerBench Authors](https://github.com/KristenZHANG/TriggerBench-Official) | `9e2185c3d071f73c195196ba1859631898017c8d` | base_context 的 temporal 3、positive state 2、positive logical 2、attention recovery 1、negative state 3、negative logical 4 |

IRC 采用的原始数据来自 Kummerfeld et al., “A Large-Scale Corpus for Conversation Disentanglement”, ACL 2019，[论文与引用](https://aclanthology.org/P19-1374/)。数据使用 CC-BY-4.0，本次裁剪对话、过滤入群事件并增加逻辑 Thread 任务标签；已保留 [原始数据许可证](licenses/IRC-CC-BY-4.0.txt) 与 [仓库许可证说明](licenses/IRC-LICENSE.md)。其他来源的 MIT 文本保存在 `licenses/`，并保留原作者署名。TriggerBench 的样本含原始生成文本中的编码瑕疵，未伪造为高质量真人聊天。

## 公开题改编

- TIAGE 保留源对话前缀和原 topic-shift 字符串标签；候选 Thread 只由当前 turn 之前的内容建立。原生 0/1 不直接等于 HeyBuddy 的 continue/new。
- IRC 保留当前消息、之前的技术对话和参与者名字，引用内容不执行为命令。原候选 msg_1004 为入群事件，弃用；msg_1006 主要是对新 NTFS 问题的解释，改选原问题 msg_1005。三个题仅作个人助手技术讨论的改编，不能报告回复图聚类成绩。
- LongMemEval 使用 8 个明确状态修订。旧用户原话放入初始主题，之后原话作为新证据；问句、答案、answer_session_ids 和 has_answer 标记从输入中移除。这里通过源标记筛选了证据，没有原题大量干扰信息，因此只能评更新决策。
- MemoryAgentBench 不将合成事实冒充现实；把原关系记录变成用户需要整理的合成世界笔记，并保留顺序。前三题更新，最后一题新建；原生 QA 答案与问题只保存在来源记录，不送给模型。未使用通用多跳 QA、长上下文重复扩展或小说后续事件预测。
- TriggerBench 保留截至决策时的全部用户 turn，移除 function/variant、意图 summary 和 ground_truth；候选使用中性标题。负例原记录仍带正例参考意图，绝不作为模型提示。两道午夜正例被 HeyBuddy 安静窗口重标 silent，原正例标签不变。

## 从原始来源重建摘录

运行评测和普通重建不需要下载原数据。若核查上游，按每条记录的固定 `source_ref` 下载到被忽略的 `.cache/bench_sources`，保持下列缓存名；先核对记录里的 SHA-256。

| 缓存文件 | 固定源中的文件 |
|---|---|
| `tiage.json` | `data/personachat/anno/test/anno_test.json` |
| `irc.txt` / `irc.annotation.txt` | `data/test/2005-07-06_14.ascii.txt` / `.annotation.txt` |
| `longmemeval.json` | `longmemeval_oracle.json` |
| `mab.parquet` | `data/Conflict_Resolution-00000-of-00001.parquet` |
| `benchmark/prospective_memory/data/base_context/...` | TriggerBench 相同相对路径 |

`hf_revisions.json` 是两个 Hugging Face repo ID 到上表 revision 的映射。MAB 用一次性临时 `uv run --with pyarrow` 读取 Parquet 的 `to_pylist()`，将 JSON 写到同目录 `mab_rows.json`；pyarrow 不是运行依赖，也没有写入项目锁文件。

```powershell
uv run python evals/heybuddy_bench/public_sources/curate_public.py --sources .cache/bench_sources
uv run python evals/heybuddy_bench/build_dataset.py
uv run python evals/heybuddy_bench/validate.py
```

`curate_public.py` 的 ID 清单固定，不根据模型分数重新选择；旧 `select_public_cases.py` 仅供探索候选。保留 35 个公开样本后，Routing 缺两题、Memory 缺八题，用明确的自建反例与边界题补足，详见 `build_dataset.py.replacements()`。当前审核者是编码 Agent，不能将其写为独立人工审核通过。
