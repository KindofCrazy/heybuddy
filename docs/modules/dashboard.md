# Observatory：本地只读工作台

## 职责与架构组成

`dashboard/app.py:create_app` 接受已装配的 `runtime.ChatService`，路由只保留查询投影。`dashboard/repository.py:ObservatoryRepository` 绑定当前 Agent，使用 `SQLiteStore.reading()` 在原有连接和事件循环上执行有界 SELECT，并复用 Episode/Thread 记录校验；不开放 SQL、Provider、Agent 或 Host 命令。`dashboard/memory.py:MemoryView` 负责路径受限的文件展示。`dashboard/server.py:dashboard_server` 管理一个 Uvicorn task，不创建第二个数据库 Owner、不接管进程信号。

`layout.py:CARDS` 只声明布局元数据；`templates/components/cards.html:base_card`、`components/drawer.html` 分别提供卡片与原生 modal dialog。`static/components.js` 构建卡片、状态、空态、元数据；`details.js` 展示关系与消息；`memory-character.js` 展示 Markdown 和 P3；`inspectors.js` 管理 URL、Breadcrumb 和 Drawer；`monitor.js` 展示状态和事件；`overview.js` 管理有界刷新。CSS 实现 12 列不对称 Bento、黄色画布、粗边框和硬阴影，提供移动端重排、离线字体 fallback、键盘焦点和 reduced-motion。

## 代码实际运行路径

1. `main.py:main` 解析 `web` 或 `qq --dashboard`、`--dashboard-port`（默认 8765，1024–65535）。不提供 host 配置，固定 `127.0.0.1`。
2. QQ 的 `channels/qq/runner.py:run_qq` 在原 `open_application()` 内通过 `AsyncExitStack` 启动 HTTP，再启动原 consumer；二者收到同一个 ChatService、Agent Scope、SQLite Owner 和 MemoryFS。
3. 独立 `run_web` 调用相同 `open_application(settings, observer_only=True)`。仍初始化 SQLite schema/会话选择并取得原 Owner 锁，但跳过未完成 Run 恢复、Memory 初始化/迁移、Automation/Organizer/Learning 回执恢复、Curator 调度与 Character State 初始化；不安排维护。`ChatService.run` 拒绝 observer-only 执行，`after_reply` 不调度任务。已有实例持锁时拒绝启动。
4. FastAPI handler 均为 async，保持 SQLite 在线程归属正确的 event loop 上。`reading()` 只供受信任固定 SELECT，复用 SQLite VM 工作预算，不开启写事务、不 await。列表按 rowid 倒序、整数 cursor、limit 分页；默认不读正文。
5. 点击卡片后 `openObject` 将 `[kind,id]` 链编码进 fragment，`route` 选择详情渲染器。Thread 用 `/api/episodes?thread_id=` 查询真正归属；Episode 用 `/api/runs?episode_id=` 读取实际 Run 元数据与来源 Highlights；Run 详情读取元数据和一页 `/messages`，默认列出用户/助手/工具消息标题，正文逐条折叠，不再增加总 reveal 按钮。工具调用/结果保留 call_id，不执行历史工具，不返回 opaque Provider/reasoning 数据。
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

## 默认中文与语言切换

界面默认中文，页头和详情抽屉内的按钮可切换 English / 中文；选择保存在浏览器 localStorage（`heybuddy.observatory.language`），刷新后恢复，仅接受 `zh-CN` 与 `en`。存储不可用时仍允许当前页面切换，未设置时使用中文。

`static/zh-CN.json` 是 UI 中英文对照，英文原文作为键；首页由 `app.py:create_app` 使用同一词典预渲染中文，模板宏通过 context 读取翻译函数，无需等 JavaScript 才显示中文。`i18n.js` 只管理显式标记的界面文案：`ui` 创建文案值，`bindText/writeText` 使用 WeakMap 绑定 DOM，并以 textContent 更新。普通 `node` 文本保持原样；聊天、Markdown、模型生成标题、路径、ID 和审计 JSON 不做翻译。动态状态/按钮/元数据标签在各渲染模块显式绑定，日期格式跟随选定语言。

语言切换直接更新当前 DOM 的文本和 aria-label，不触发 HTTP 业务操作、重新加载对象或重建 Drawer，因此保留已展开消息、滚动位置和 fragment。`tests/test_dashboard_language.py` 验证无 JavaScript 时的中文首页及静态资源；真实 Edge 验收覆盖切换、偏好恢复、存储拒绝容错、详情/正文保持与移动端宽度。

## 栏目独立页面与页头布局

`layout.py:PAGES` 定义四个固定页面：总览 `/` 保留 Bento 概览，`/threads` 展示有界分页的主题与片段，`/memory` 展示 MemoryFS 及学习回执，`/runs` 展示有界分页运行列表。`app.py:home` 的固定 GET 路由按路径选择卡片和标题；顶部使用普通链接进行文档导航，不再使用滚动锚点，`aria-current` 与当前路径一致。直接访问、刷新和浏览器后退均沿普通 HTTP 页面语义。默认语言和本地偏好保持不变。

`overview.js` 按 `body.dataset.page` 装配当前页面；专页复用 `details.js:cardsPage`，按需读取后续页，总览保留现有低频概览刷新，所有页面继续更新页头连接状态。`monitor.js:replace` 在当前页无对应卡片时跳过渲染，避免查询别页内容或访问空 DOM。Inspector 的 `close` 清除当前路径的 fragment 并执行现有路由清理，保留所在栏目和滚动位置。

模板把状态和语言按钮放在同一个 `.header-controls`，顺序为连接状态 / 本机标记 / 语言按钮；按钮使用浅蓝色、紧凑尺寸、黑边和硬阴影，悬停为浅紫色，移动端保持同组排列。抽屉内语言按钮保留。`tests/test_dashboard_navigation.py` 验证四页内容、链接、当前页标记、控件顺序和读取无副作用。Edge 验证真实文档导航、语言持久化、详情关闭、刷新/后退、按钮位置配色与四页移动端无溢出。

## 总览精简、首屏语言与视口适配

总览现在只呈现 Agent Status、Activity、Character、Background Jobs，Thread/Memory/Run 列表只在对应栏目读取，不再占据总览。`layout.py:PAGES` 选择这四张卡片，`overview.js` 不再请求总览的 Thread/Run 列表或 MemoryFS。

为避免跨页面时中文先显示、随后变成英文，`app.py:home` 只接受 `heybuddy_language` cookie 的 `en` / `zh-CN` 值，并使用共享 `en.json` / `zh-CN.json` 在服务端输出首屏语言。`i18n.js:setLanguage` 同步更新浏览器 localStorage 与仅含语言枚举的 SameSite cookie，不涉及数据库或 Memory 写入。`language-bootstrap.js` 在 CSS 前以普通同源脚本运行，将旧 localStorage 偏好迁移到 cookie；若旧偏好与服务器语言不同，先隐藏错误语言的工作区，待界面翻译完成再显示。正常后续导航由服务端直接输出正确语言，无需等翻译模块才纠正首屏。现有 CSP 保持禁止内联脚本。

CSS 使 `.shell` 占据 `100dvh`，以 flex 给页头、精简标题和页脚预留空间，Bento 使用剩余高度和 `minmax(0, 1fr)` 网格。桌面总览左侧状态/角色上下排列、中间活动、右侧 Jobs；窄屏为 2×2，专页根据宽度切换并列或上下两张卡片。卡片正文独立 overflow，不再撑长文档；短窗口隐藏次要装饰。活动和 Jobs 的预览数量、专页首次页大小按视口预算限制，后续内容继续使用已有分页或 Inspector，不删减原始数据。

`tests/test_dashboard_language.py` 新增带语言 cookie 的四页首屏验证和非法语言回退；导航测试更新总览四卡契约。Edge 在 1440×900、1366×768、1024×600、768×900、390×844、844×390、320×568 下检查四页文档无纵横向溢出且卡片正文有可用高度；阻止翻译模块加载验证服务端英文首屏与旧偏好保护，未捕获 JS 异常。长内容仍可在卡片或 Drawer 内滚动，保持字号可读，不声称无限历史可同时铺在一屏。

## 稀疏记忆阅读与图标

总览模板在 `for` 循环中限制四个允许卡片 ID，CSS 同时隐藏旧上下文中的 Thread/Memory/Run 卡片，兼容旧 Runtime 的七卡 context 与热更新模板/资源混用；最新 PAGES 仍仅提供四卡。阅读测试模拟旧 CARDS context，验证 HTML 不产生多余卡片。标题删除原白底黑边句点装饰，避免看起来像未知控件。

`static/memory-workspace.js:memoryWorkspace` 在 Memory 栏目创建受限文件导航与内联阅读区。首次仅请求根目录的一页合法条目，默认读取 MEMORY.md，导航目录时维持有界分页；文件选择调用原 `memory-character.js:memoryFile`，展示正文、Hash、修改时间与已有学习记录，完整详情仍打开原 Inspector。只读取已选文件，不递归扫描、不做写入、修复或虚构统计。并发文件读取用 generation 检查丢弃旧响应。基础文件附有用途标签；无正文或无学习记录显示明确空态。`MemoryView.file` 只在 HTML 展示中规范化 CRLF 并移除完整 Frontmatter，原 content、Hash 与 Markdown 字节不变。

导航、卡片、活动和文件使用本地 [Lucide](https://lucide.dev/) SVG 子集。`templates/components/icons.svg` 包含固定 symbol，模板宏和 `static/icons.js:icon` 只引用允许名称，不渲染用户提供 SVG。无需放宽 CSP，无远程字体、图片或脚本。原始 ISC/MIT 版权与许可保存在 `static/ICONS-LICENSE.txt`，随 Python 包分发，来源见 [官方许可证](https://lucide.dev/license)。不依赖 Flaticon 账号或在线资源。

`tests/test_dashboard_presentation.py` 验证旧布局上下文、标题装饰删除、图标许可资源和 LF/CRLF Frontmatter 的显示/原文分离。真实 Edge 验证图标非空、默认索引、目录与文件切换、完整来源详情、安全 Markdown 以及桌面/手机布局，截图仅使用合成数据。

## 消息列表与模型轮次

运行详情自动加载一页消息，用户、助手、工具名称作为主要标题，`sequence` 以“存储序号 #n”次要标注。它是整个 Conversation 中包含工具消息的稳定顺序，不表示助手调用轮次。单条正文保持原生 details 折叠，后续消息仍用有界分页。原隐私说明和总展开按钮已删除，hb 标记上的星形装饰也已移除。

“模型轮次与 Prompt”链接进入 `/runs#inspect/rounds/{id}?full=1` 的完整详情视图，可直接刷新。`inspectors.js` 分派给 `model-rounds.js:modelRounds`，先读轮次元数据，按需读取输入/返回快照。输入包含调用当时的 Base、动态注入、选中历史、当前 Run 消息、工具定义和输出上限；时间、耗时与 token 数独立展示，正文不随语言切换改变。快照分段读取、错误可重试，后续轮次有界分页。旧 Run 缺失时显示明确空态，不按当前 Memory 伪造历史。保存路径及边界见[模型调用审计](tracing.md)。
