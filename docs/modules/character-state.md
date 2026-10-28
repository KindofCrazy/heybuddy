# Passive Character State

## 职责与组成

`character/state.py::CharacterStateService` 在已有 Idle 维护轮中执行一次空工具状态建议，不聊天、不执行工具、不写 MemoryFS 或角色包。StatePolicy/StateProposal/CharacterState 位于 character/models.py。`character/storage.py::CharacterStateRepository` 用 schema 12 追加角色 Run 归属、单个当前状态和来源处理回执三张小表；1–11 不改，没有人格正文副本、情绪历史轨迹或关系数据库。

## 真实执行路径

1. main._run_session() 只在启用角色时绑定状态服务和角色编译器。Core ContextBuilder.begin() 创建原 Run 后调用可选 record_run()，Host 记录 agent_id/character_id/package_hash；原 Run/Message 不改。标签失败时暴露安全诊断，未标记来源不能作为该角色的状态证据，不伪造归属。
2. Repository.candidate() 仅找有该 Agent/Character/包 hash 标记 Run 的新/修订 Episode。snapshot() 重读实际归属、完整 completed Run 和大小，最多 16 个来源、默认 32768 字节。混合角色 Episode 只提供当前角色自己的 Run，摘要只定位，不是事实证据。
3. ready() 受默认 900 秒最小间隔约束。Runtime 每轮最多运行一次 State Stage，空闲、无 running Run、有预算且不争抢 P2 优先轮才执行。默认输入 24576 估算 tokens、输出 2048 tokens、20 秒和 4096 响应字节；共享原 MaintenanceBudget/CycleProvider 与整轮 4/4/131072/120 上限。
4. 新来源的签名基于实际 Run 内容与时间；状态 Finalize 仅改变 Episode revision 而签名未变时保存零请求 no-op，不重复付费。旧、过期或未来时间不可信的事件也不刷新状态。模型读取角色身份/Behavior/旧有效状态/真实来源，允许 no_op；没有每轮强制 Appraisal 或固定情绪映射。
5. update 必须引用实际提供的 conversation_id/run_id/sequence 和精确文本，复用 Reflection 引文校验。Observed Time 来自实际所引新 Run 的 ended_at，不是处理时间；只引用旧事件不能给 Mood 续期。发布前重读签名和 Episode revision，CAS 检查状态版本、来源顺序和时间；状态与来源 completed 在同一 SQLite 事务提交，旧结果不能覆盖更新状态。
6. 取消恢复 pending；失败保留 failed 并需显式 retry；重启将遗留 processing 纯建议恢复 pending，不重放历史工具。包 hash 改变会清空旧状态并推进 revision，旧包标记的 Run 不被新包自动重解释。不同 Agent/Character 分别读取自己的行。

## 有效期与注入

active() 默认一小时后将 Mood 视为 neutral，三小时后清除 Focus/Stance，再没有有效字段时省略状态。年龄按来源事件计算，进程内结合 monotonic，时钟回拨不能续命；重启依据可靠 UTC 恢复。过期只改变读取视图，不删除 SQLite 记录或 Journal。状态作为低信任 user JSON 注入，不能改稳定身份、Host 权限、当前请求或事实。

即时连续性仍靠原 Recent Runs。State 是有界的近期行为模拟，不是长期用户事实；长期关注、稳定偏好与共同经历仍由 P1/P2 处理。状态失败或暂无有效状态时 Main Agent 使用角色基线与近期历史，不能因此编造情绪或经历。

## 配置、诊断与验证

角色默认关闭；启用 E2 后，AGENT_CHARACTER_STATE_ENABLED 控制更新。E0/E1 禁用动态状态调用。其余字段见 .env.example：MOOD_SECONDS、FOCUS_SECONDS、MIN_INTERVAL_SECONDS、INPUT_TOKENS、OUTPUT_TOKENS、TIMEOUT_SECONDS、SOURCE_BYTES，均使用 AGENT_CHARACTER_STATE_ 前缀。

`/character status|run|retry` 是可信调试入口，run/retry 只处理一个有界来源，仍受共享预算；不主动输出角色消息。/episode status 显示状态阶段与安全计量，/context status 显示角色注入大小与状态是否可用，不打印私人 reason/引文。无新事件不请求模型，模型用量由 character_state 阶段单列。

tests/test_character_state.py 验证引用、no-op、Finalize 去重、Mood/Focus 过期、回拨、角色隔离、取消/CAS、真实 SQLite 重开、包变化、限流、旧引文不刷新，以及真实 Main Agent Run 标签且不写入角色 System。tests/test_character_storage.py 验证 11→12 原子回滚、Agent 外键触发器和状态/来源记账失败整体回滚。P2 的调度和存储回归继续保留。
# Observatory 读取

`CharacterStateRepository.row()` 改用 `SQLiteStore.reading()` 执行有界 SELECT，不取得写事务。Dashboard 复用 `CharacterStateService.active()` 的 TTL 和实际字段；当前/过期存储状态与 source-attempt receipts 分开显示，不建立旧状态正文副本。HTTP 不调用状态生成。验证见 `tests/test_dashboard.py:test_character_state_uses_actual_p3_model_and_receipts`。


## 独立观测模式

`CharacterStateService(..., initialize=False)` 跳过状态行创建、包指纹变更清理和 pending 恢复，仅独立 web 装配使用。`CharacterStateRepository.exists()` 只读检查当前 Agent/Character/Package Scope，未形成状态时显示空态。普通聊天默认初始化行为不变。验证见 `tests/test_dashboard_lifecycle.py`。
