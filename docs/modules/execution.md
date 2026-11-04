# 工作文件、Subagent 与后台执行

## 职责与架构组成

`tools/workspace.py::Workspace` 提供专用工作文件与固定命令；`execution/process.py::ProcessTree` 管理子进程树；`core/text_window.py::text_window` 保证 UTF-8 分页不拆断字符；`subagents/service.py::SubagentService` 负责深度一委派；`execution/service.py::ExecutionService` 保存排队与运行状态。

默认工作区在用户数据目录 `HeyBuddy/workspace`；可通过 `AGENT_WORKSPACE_ROOT` 设置。Memory、角色、Skills 和 MCP 配置路径受保护，不授予它们普通文件写权限。可选 `AGENT_WORKSPACE_GIT_ROOT` 仅授权固定 Git 状态查询。

## 代码实际运行路径

1. Workspace 工具将相对路径映射到 Host 根目录，拒绝绝对路径、穿越、隐藏路径、平台保留名、链接、保护目录、凭据/配置名称和非工作文档类型。
2. list/search 进行有界扫描；read 返回内容 hash 与分页偏移。write/edit 在短 asyncio 锁中校验 expected_hash，用同目录临时文件 flush/fsync 后发布。创建使用排他链接防覆盖，替换前再次核对原文；没有跨进程安全沙箱保证。
3. `command_run` 只接受 text_search 或 git_status。前者使用有界文本检索，后者采用固定 argv、关闭 fsmonitor、禁可选 Git 锁和最小进程环境；不能提交命令文本或脚本。
4. Git 子进程受 5 秒等待限制和输出上限约束。Windows 使用 kill-on-close Job Object；POSIX 使用独立进程组。取消/超时等待所属进程退出，不重试命令。
5. `delegate` 检查根执行次数，创建独立 execution Agent/Conversation，复用原 Agent Loop、Provider 和 SQLiteConversationHistory。使用独立 Agent 身份使既有 Agent 归属查询排除子执行的合成用户输入。
6. 子 Agent 仅注册时间、工作文件只读工具与 Skills；没有 delegate、Memory 写入、提醒、命令和 Host 配置工具。最多四轮、深度一、每根两次，使用根预算，返回精简结果与独立 Journal 标识。
7. `execution_start` 只能启动已注册的 delegate 或 workspace_search。状态先持久化为 queued，再启动 asyncio Task；最多两并发和 16 排队。后台模型请求等待前台让出容量。
8. worker 进入 running，执行操作后保存结果或安全错误类型。cancel 发出取消并等待清理；结果通知先留在数据库，下次同来源对话装配，成功处理后仅确认实际展示的通知。
9. 重启只将遗留状态标 interrupted。没有进程 PID 续认、自动重发或重新执行；QQ 不新增后台完成主动发送。

## 异常与边界

文件类型只允许 `.md/.txt/.json/.csv/.tsv`，单次写入最多 64 KiB，读取单文件最多 1 MiB。正文检索最多扫描 200 项、返回 30 个片段；目录列表最多 100 项。

`expected_hash` 与短锁不能阻止本机恶意用户在检查与发布之间制造竞争。子执行的来源列表由任务显式提供，简短结论仍应按数据审阅；父 Agent 不把子结论当用户事实。

## 阅读与验证

`execution_start(operation="workspace_search", arguments={"query":"资料"})` 返回作业 ID；使用 execution_status/result/cancel 查询或取消。`tests/test_harness.py` 覆盖精确编辑、路径拒绝、进程归属、父子隔离、共享预算、通知确认和后台取消。
