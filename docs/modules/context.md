# Context：Provider 输入编译器

## 职责与架构组成

`src/personal_agent/core/context.py::ContextBuilder` 编译规则、Memory、角色、历史与当前 Run，通过 core/history.py 的 ConversationHistory Protocol 访问历史。core/budget.py 估算窗口，core/replay.py 生成派生执行视图，retrieval.py 提供可选证据。

## 代码实际运行路径

1. SessionManager.context 绑定 Conversation；Harness.bind_context 安装 ReplayView、Planning/Skills/Execution Contributors 与 on_begin。
2. begin 保存原输入并重置检索、投影状态。pending 保留本 Run 原消息。
3. prepare 首次 _load 读摘要与有界完整 Run 页；_retrieve 每 Run 一次，使用原始用户文本，排除近期/当前 Run。Preflight 可能增加模型请求。
4. 每次循环从 page + pending 重建工作消息，然后投影当前 Run。_injections 重新编译 Memory、角色、摘要、时间、通知和贡献器。
5. _select_history 的可选额度取 optional_history_tokens、输入上限三分之一、触发水位剩余三者最小值。按 Run/call_id 去重；语义 Memory 按路径/section_hash 去重。只有覆盖的 Memory 真正装入后，才省略相同 Thread 状态。
6. _assemble 按 system/注入数据→可选证据→近期页→当前 Run 拼接，计入工具定义估算，必要时压缩后重建。
7. record 先写 History 再拷贝到 pending；commit 保存终态后清空 pending；abort 标中断/取消并清理。

精确注入顺序见[消息主线](../architecture.md)。for_request 只编译注入，不代替 prepare 的分页、检索、预算；build 也是简化接口，不是生产完整准备路径。

## 加载、编译与选择为什么分开

`_load()` 先读元数据和一页 payload，默认每页最多 32 个 Run。ContextBudget.load_bytes 同时考虑回答与摘要预算，最大 2 MiB；完整 Run 必须在读取边界内，不能靠加载后随意切掉工具结果来满足预算。会话摘要通过 through_sequence 指向已压缩前缀，下一页从这个边界继续。

`_retrieve()` 解决“哪些旧证据可能相关”，不是最终入选。确定性 retrieve 成功后会先保留结果，再将深拷贝交给可选 preflight；后者失败不抹掉基线。`_select_history()` 才解决“哪些相关证据放得下”，默认 optional_history_tokens 为 12288，还受三分之一窗口和剩余额度约束。省略可选证据与压缩原始近期历史是两种行为，不能混作“历史丢失”。

`_injections()` 每次 prepare 迭代重新编译一次；该迭代的估算与发出请求共享同一份编译结果，避免预算检查和实际发送之间读取两份不同 Memory。角色编译接收最多 32 个当前、检索及近期 Run 引用；这些来源标签不改变写权限。图片只以 ImageRef 留在消息中，估算另按每图 4096 预留，不把 Base64 混入历史索引。

例如第一轮读取主题、工具修改了获授权的 Memory、第二轮生成答案：原用户输入仍是同一个 pending[0]，新 Memory 在第二轮重新 compile；首轮的检索快照通常沿用。最终 Journal 保存原输入、模型调用与工具结果，不保存自动注入的第二轮 Core JSON。

## 异常与边界

Memory 每轮刷新，检索通常每 Run 一次。参考数据以低信任 user JSON 注入，不写进 Journal。每项贡献器上限 32 KiB，全请求还要过窗口预算。检索异常可降级，Core/Journal/预算异常停止。history 属性是有界工作页，不是全量审计 API。completed/failed 可重放，其他状态仅保留审计。

## 阅读与验证

`uv run pytest tests/test_working_context.py tests/test_memory_runtime.py tests/test_compaction.py`。观察 Fake Provider 收到的消息，核对注入不落 Journal、完整 Run 选择与 Provider JSON 保留。

## 未来改进

- 统一记录每层来源、大小和省略原因，使模型输入可解释。
- 为贡献器提供显式 Protocol、总额度和优先级。
- 评估 Memory 改变后是否需要有界刷新本 Run 的检索快照。
