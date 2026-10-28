# Observatory：本地只读工作台

## 职责与架构组成

`dashboard/app.py:create_app` 接受已装配的 `runtime.ChatService`。`dashboard/repository.py:ObservatoryRepository` 通过 `SQLiteStore.reading()` 在原有数据库连接和事件循环上执行有界 SELECT，复用 Episode/Thread 的记录校验。路由不暴露 Provider、Agent 或宿主命令。`dashboard/server.py:dashboard_server` 管理单个 Uvicorn task，不创建数据库 Owner，不接管进程信号。

## 代码实际运行路径

1. `main.py:main` 解析 `web` 或 `qq --dashboard`、`--dashboard-port`（默认 8765，1024–65535）。不提供 host 配置，固定 `127.0.0.1`。
2. `web` 调用 `run_web`，通过唯一 `runtime.open_application()` 装配应用；不安排维护。QQ 在 `channels/qq/runner.py:run_qq` 的同一个 `open_application()` 作用域中启动 HTTP task，再启动原 QQ consumer。
3. `create_app` 将当前 Agent 固定绑定到只读 Repository。GET 列表按 SQLite rowid 倒序、整数 cursor 和 limit（最多 20）分页；不读取聊天正文。GET 单对象重新校验 Agent 归属。
4. 请求均在 async handler 中执行，短 SELECT 不 await、不持写锁；SQLite VM 查询预算限制工作量。HTTP 展示投影统一经过 `privacy.safe` 脱敏和 512 KiB 输出预算。
5. 退出时先关闭 QQ，再停止 HTTP 接收、等待有界 graceful shutdown，最后关闭共享 Runtime 和 SQLite Owner。

## 异常与边界

## 视觉与组件

`layout.py:CARDS` 只声明卡片身份、标题与标签；`templates/components/cards.html:base_card` 渲染公共结构，`components/drawer.html` 提供独立原生 dialog。`static/observatory.css` 实现 12 列不对称 Bento、黄色画布、黑色粗边框与硬阴影、移动端重排和 reduced-motion。字体均有离线 fallback，无 CDN。`components.js` 用 `textContent` 构造可复用卡片、状态、空态和元数据，按钮阻止冒泡；`overview.js` 从真实 GET 列表加载概览。布局与对象投影不耦合。

## 请求错误

## 嵌套详情实际路径

`overview.js` 的卡片调用 `inspectors.js:openObject`，将 `[kind,id]` 链编码进 URL fragment。`route` 在原生 modal dialog 内构造 Breadcrumb，并把当前节点分派到 `details.js:objectDetail` 或 `memory-character.js`。Thread 用 `/api/episodes?thread_id=` 查真正归属；Episode 展示经 Repository 校验的 Run IDs 与 Highlights；Run 的初始 GET 仅取元数据，点击 reveal 才分页读取 `/messages`，每条消息继续折叠。后台概览刷新不触碰 dialog；较早返回的异步详情由 requestVersion 丢弃。ESC、Close、Workspace 或浏览器返回均沿 URL 状态关闭/回退；`?full=1` 展开完整宽度，刷新保持路径。

Memory 逐目录查询 `/api/memory`，由 `MemoryView.listing` 有界扫描当前目录（最多 MemoryFS 的 MAX_ENTRIES），先 guard 再判断类型，跳过非法路径/链接；不递归、不写入、不修复。`/api/memory/file` 使用原 `MemoryFS.path/raw`，按字符 offset 分段并用 expected_hash 检查后续片段；本地文件 I/O 在只读工作线程中进行，不持写锁。`markdown-it-py` 禁止 HTML、图片、链接，前端 `prose` 再复制无属性标签白名单。文件详情经 `/api/memory/records` 展示真实 Learning 回执；无旧正文则无 Diff。

`/api/character` 使用已加载 Character Package；`/api/character/state` 复用 `CharacterStateService.active()` 的实际 TTL 与 mood/current_focus/stance，分别返回 current effective、last stored 和来源 attempt receipts。`CharacterStateRepository.row` 使用 `SQLiteStore.reading()`，HTTP 不创建状态。旧状态正文没有历史副本，界面明确说明；角色关闭不虚构字段或事件。测试见 `tests/test_dashboard.py` 的关系、隐私、Memory 和 P3 用例。

对象不存在或不属于当前 Agent 返回 404；参数错误返回固定 422；存储失败返回固定 503，不返回异常正文。只允许 GET/HEAD，禁用 API 文档写入口、跨站请求和不匹配 Host。所有页面禁止缓存，设置 CSP、no-referrer、nosniff；无远程资源和 CORS。监听端口冲突在启动 consumer 前失败，运行时 HTTP 异常不传播到 QQ task。

## 阅读与验证

## 实时观测与刷新

`OrganizationRuntime._execute()` 在现有 Stage await 前后记录有界 `events`（deque，最多 128 条，仅时间、Stage、结果和关联类型，不存正文）；取消/失败保持原传播语义。`diagnostics(..., read_only=True)` 走 `_observe`，只读已有 schedule/progress、封顶 pending 计数和最近失败，不调用 ensure/poll/LLM。`ChatService.run` 在 try/finally 维护真实 foreground_active；QQGateway 的 connecting/ready/reconnecting/error/stopped 来源于连接生命周期，`run_qq` 仅绑定只读状态 callback，不暴露 token/OpenID。

`activity.py:activity` 每类仅取最近 64 条元数据，合并真实 Run 起止、Learning/Curator 最新回执、当前 Character State 和当前进程 Stage 事件；按 `(timestamp,event_id)` 稳定分页，cursor 编码该键。Episode/Thread 没有独立发布时间，明确作为 snapshot 并标注 source time。历史阶段事件不伪造恢复；重启后只恢复现有持久事实。接口标记 scope_limited，不是完整历史事件账本。

`monitor.js` 每次只渲染状态/近期事件；`overview.js` 串行等待一轮后 setTimeout 3000 ms，隐藏页面暂停，无重叠轮询。Thread/Run/Job/Character 概览每五轮刷新，Memory 树只在首次或明确导航读取。签名相同或控件持有键盘焦点时不替换 DOM，任何轮询都不写 Inspector、fragment 或当前详情内容。`tests/test_dashboard.py` 覆盖分页、真实 Stage、取消清理和无写入保证。

`uv run personal-agent qq --dashboard` 或（先退出其他本地实例）`uv run personal-agent web`，访问 `http://127.0.0.1:8765`。独立 web 仍使用既有 Owner 锁，已有实例运行时明确拒绝，不开第二个写实例。`tests/test_dashboard.py` 使用临时 SQLite、MemoryFS、FakeProvider 和 ASGITransport，验证分页、隔离、只读和安全请求边界。无需真实 Key 或 API 请求。
