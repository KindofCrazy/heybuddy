# 共用证据投影与精确引用

## 职责与架构组成

`core/evidence.py` 只依赖内部模型和 HistoryTurn，包含 Evidence、EvidenceError、SECRET/redact、projection、history_evidence 和 verify_quotes。它不认识 SQLite、MemoryFS、角色包、计划、审批或 Provider。Evidence 保持原四字段与长度约束；Reflection 从原 models 模块重导出同一个模型以保留 Schema 形状。Episode 的 EvidenceRef 仍有独立单字引文规则。

`reflection/evidence.py` 将共用错误转换为 ReflectionError，并在 verify_candidates 中增加 confirmed 必须含真实 user 引文的记忆策略。`reflection/provider.py::ReflectionModel` 独立负责原 Reflection 结构化请求的预算、超时、取消屏障、usage 与 JSON 校验；不会生成/保存计划或写 Memory。各领域继续拥有自己的请求预算和语义策略，不统一成通用 Agent 或安全框架。

## 代码实际运行路径

1. `runtime.assemble_chat()` 装配 ReflectionService 与 Learner，各自持有独立 ReflectionModel，复用同一底层 Provider；CharacterStateService 直接接收 Provider，使用原 OrganizerModel。
2. Reflection/Learning 的 `memory_evidence(conversation_id, turns)` 调用 history_evidence，验证 Run 的 sequence 连续性，生成与原请求相同的脱敏文本/工具投影，并补真实会话与 sequence。Provider reasoning 不进入投影。
3. ReflectionModel.ask() 通过 structured_request() 保持原 system + user JSON/Schema 封装；调用前检查输入估算，空工具请求后重验超时/取消、输出 token、可见字节、凭据模式和结构。错误保留原阶段及安全 usage，不输出私人正文。
4. 候选返回后 verify_candidates() 调用 verify_quotes(refs, sources)。来源映射键是宿主实际提供的 `(conversation_id, run_id)`，逐条检查 sequence 与实际披露文本中的精确 quote，拒绝伪造或含 REDACTED 的引文；返回是否有真实 user 引用。confirmed 无 user 来源时拒绝。
5. Character State 使用相同 history_evidence/verify_quotes，但不构造长期记忆 Candidate，也不套用用户确认策略。角色来源标签、package fingerprint、时间与 TTL 由原 State Service/Repository 验证。
6. 通过验证后各服务继续原计划或状态发布路径；校验器本身没有写入能力，也不能授予任何 Memory 权限。

## 异常与边界

EvidenceError 只携带固定 code；记忆调用边界转换为 ReflectionError，角色调用边界保存 State 的失败回执。校验证明来源和披露引文存在，不证明模型的语义推断成立，不替代 Reflection 人工审批。工具投影仍限 1024 字符、工具参数 512 字符；拒绝/脱敏/展示策略由各域分别组合，不宣称完整秘密识别。

## 阅读与验证

最小纯调用：从已知 HistoryTurn 构造 `sources={(conversation_id, turn.run_id): turn}`，用 `verify_quotes([Evidence(...)], sources)` 校验精确引文；不存在的 Run 或越界 sequence 抛 EvidenceError。请求层用 FakeProvider 返回既定 JSON，通过 ReflectionModel.ask() 验证空工具与预算。

`tests/test_reflection.py` 验证伪造引文、assistant-only、脱敏与预算；`test_learning.py` 验证多会话来源；`test_curator_evidence.py` 验证目标文件的来源范围；`test_character_state.py` 验证角色来源、取消与 TTL。它们是通过实际业务发布路径的回归，不需要真实 Key 或模型费用。
