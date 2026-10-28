# Observatory：本地只读工作台

## 职责与架构组成

`dashboard/app.py:create_app` 接受已装配的 `runtime.ChatService`，路由只保留查询投影。`dashboard/repository.py:ObservatoryRepository` 绑定当前 Agent，使用 `SQLiteStore.reading()` 在原有连接和事件循环上执行有界 SELECT，并复用 Episode/Thread 记录校验；不开放 SQL、Provider、Agent 或 Host 命令。`dashboard/memory.py:MemoryView` 负责路径受限的文件展示。`dashboard/server.py:dashboard_server` 管理一个 Uvicorn task，不创建第二个数据库 Owner、不接管进程信号。

`layout.py:CARDS` 只声明布局元数据；`templates/components/cards.html:base_card`、`components/drawer.html` 分别提供卡片与原生 modal dialog。`static/components.js` 构建卡片、状态、空态、元数据；`details.js` 展示关系与消息；`memory-character.js` 展示 Markdown 和 P3；`inspectors.js` 管理 URL、Breadcrumb 和 Drawer；`monitor.js` 展示状态和事件；`overview.js` 管理有界刷新。CSS 实现 12 列不对称 Bento、黄色画布、粗边框和硬阴影，提供移动端重排、离线字体 fallback、键盘焦点和 reduced-motion。

## 代码实际运行路径

1. `main.py:main` 解析 `web` 或 `qq --dashboard`、`--dashboard-port`（默认 8765，1024–65535）。不提供 host 配置，固定 `127.0.0.1`。
2. QQ 的 `channels/qq/runner.py:run_qq` 在原 `open_application()` 内通过 `AsyncExitStack` 启动 HTTP，再启动原 consumer；二者收到同一个 ChatService、Agent Scope、SQLite Owner 和 MemoryFS。
3. 独立 `run_web` 调用相同 `open_application(settings, observer_only=True)`。仍初始化 SQLite schema/会话选择并取得原 Owner 锁，但跳过未完成 Run 恢复、Memory 初始化/迁移、Automation/Organizer/Learning 回执恢复、Curator 调度与 Character State 初始化；不安排维护。`ChatService.run` 拒绝 observer-only 执行，`after_reply` 不调度任务。已有实例持锁时拒绝启动。
4. FastAPI handler 均为 async，保持 SQLite 在线程归属正确的 event loop 上。`reading()` 只供受信任固定 SELECT，复用 SQLite VM 工作预算，不开启写事务、不 await。列表按 rowid 倒序、整数 cursor、limit 分页；默认不读正文。
5. 点击卡片后 `openObject` 将 `[kind,id]` 链编码进 fragment，`route` 选择详情渲染器。Thread 用 `/api/episodes?thread_id=` 查询真正归属；Episode 用 `/api/runs?episode_id=` 读取实际 Run 元数据与来源 Highlights；Run 初始 GET 仅取元数据，点击 reveal 才读取 `/messages`，逐条继续折叠。工具调用/结果保留 call_id，不执行历史工具，不返回 opaque Provider/reasoning 数据。
6. Memory 树只扫描当前目录（最多 MemoryFS 的 MAX_ENTRIES），先 guard 再检查类型，跳过非法路径/链接，不递归。文件使用既有 `MemoryFS.path/raw`，按字符 offset 分段、expected_hash 校验后续页；只读文件 I/O 使用工作线程，不持写锁。Markdown 渲染禁用原始 HTML、图片和链接，前端再复制无属性标签白名单。`/api/memory/records` 查询真实 Learning operations 中的目标路径，不生成旧正文或虚构 Diff。
7. `/api/character` 使用已经加载的 Package；`/api/character/state` 复用 `CharacterStateService.active()` 的真实 TTL 和 mood/current_focus/stance，区分 effective、last stored 与 source-attempt receipts。缺少初始化状态行时返回无状态，Character 关闭时无虚构字段。旧状态正文不另建副本。
8. `OrganizationRuntime._execute` 在原 Stage await 前后记录最多 128 条无正文内存事件，取消/失败仍沿原路径传播。`diagnostics(..., read_only=True)` 只读已有 schedule/progress、封顶 pending 计数和最近失败，不调用 ensure/poll/模型。`ChatService.run` 在 try/finally 维护 foreground_active；QQ 状态来自真实 Gateway 与 consumer 生命周期，不从更新时间推断任务正在执行。
9. `activity.py:activity` 每类最多读取最近 64 条元数据，合并真实 Run 起止、Learning/Curator 最新回执、当前 Character State 和当前进程 Stage 事件。按 `(timestamp,event_id)` 分页，cursor 编码该键。Episode/Thread 无独立发布时间，明确作为 snapshot 并标记 source time；接口标记 scope_limited，重启只恢复持久事实。
10. `overview.js` 一轮结束后 setTimeout 3000 ms，隐藏页面暂停，无重叠请求；其他概览每五轮更新，Memory 树首次/明确导航读取。签名相同或控件持有键盘焦点时不替换 DOM；不刷新 Inspector 或 URL。旧异步详情用 requestVersion 丢弃。ESC、关闭、Breadcrumb、浏览器返回支持回退，`?full=1` 展开全宽，刷新保持路径。
11. 退出先关闭 adapter，再停止 HTTP 接收并有界等待，最后关闭原 Runtime/Owner。端口冲突在启动 consumer 前失败；请求异常返回安全错误，不杀死 QQ。

## 异常与边界

对象不存在或跨 Agent 返回 404，参数错误返回固定 422，查询失败返回固定 503，不泄露异常正文。响应经 `privacy.safe` 限制递归、集合、文本与凭据字段，整体不超过 512 KiB。原文有界预览可能截断，完整 SQLite 审计不变。Memory 文件 hash 冲突需重新打开；跳过不合法目录项不等于修复索引。

固定监听 loopback，只允许 GET/HEAD，禁用 API 文档、CORS、跨站请求和不匹配 Host。页面设置 CSP、no-store、nosniff、no-referrer、同源资源策略；消息通过 textContent 展示。无远程字体、资源追踪或数据库下载接口。明显凭据脱敏是模式防护，不是完整 DLP。

观测服务不提供 Web Chat、Memory/Character 编辑、Repair、Retry、Host 操作或付费模型请求。实时事件不是持久账本；Learning 和 Character 均只显示现有可用回执。独立 web 对遗留 running 记录标记 Unknown，不声称进程当前正在执行。

## 阅读与验证

运行 `uv run personal-agent qq --dashboard`；或先退出其他本地实例，再运行 `uv run personal-agent web`，访问 `http://127.0.0.1:8765`。独立 web 沿用现有环境配置格式，仍需配置字段合法，但不会请求 Provider。

`tests/test_dashboard.py` 覆盖真实关系、分页、脱敏、Memory、P3 与事件；`test_dashboard_lifecycle.py` 使用本机 HTTP、Fake QQ 和 FakeProvider 验证相同服务、查询失败隔离、端口冲突、关闭清理及 observer-only；`test_dashboard_boundaries.py` 验证全类型跨 Agent、硬链接、文件分段冲突和重启。所有测试保留 `tests/conftest.py` 网络保护。浏览器与安装包验收、截图、限制详见 [Observatory 交付与验收](../observatory.md)。
