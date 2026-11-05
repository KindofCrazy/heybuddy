# Context：Provider 输入编译器

## 职责与架构组成

`src/personal_agent/core/context.py::ContextBuilder` 编译规则、Memory、角色、历史与当前 Run，通过 core/history.py 的 ConversationHistory Protocol 访问历史。core/budget.py 估算窗口，core/replay.py 生成派生执行视图，retrieval.py 提供可选证据。

## 机制流程

1. 先确定这轮必须保留的信息：程序规则、当前用户请求、尚需处理的工具结果，以及受保护的近期交互。
2. 加入最新用户记忆、角色设定、时间、任务状态和会话摘要，并区分程序规则与参考资料。
3. 从相关旧讨论和主题资料中选择补充证据，去掉重复来源；资料相关也不代表一定放得下。
4. 把消息与工具说明一起计算大小。先控制可选证据，再在需要时压缩允许缩减的历史；不能牺牲完整工具配对来凑窗口。
5. 输入可容纳才交给模型；无法安全缩减就停止。下一轮重新编译动态信息，原始聊天记录保持完整。

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

## Thread 路由后的上下文

生产 Harness 默认将 History Protocol 实现换成 `storage/thread_history.py::SQLiteThreadHistory`。`begin()` 仍原子保存原 Conversation Run；首次 `prepare()` 先 await Host 注入的 `prepare_scope(provider)`，确定 Thread 后才 `_load()`、`_retrieve()`。同一 Run 后续模型轮次只刷新贡献器，不重新路由。`active_thread` 与 `thread_working_summary` 都作为低信任 user JSON 插入 messages，原 Run 则按内部 Message 序列重放。线程摘要仅覆盖本 Thread 的完整已结束 Run，不混用 Conversation 旧摘要；原日志仍完整。具体边界和验证见 [Thread Router](thread-routing.md)。
