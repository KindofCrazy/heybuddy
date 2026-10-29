# HeyBuddy Observatory — MVP 交付与验收

## 运行

```sh
uv run personal-agent qq --dashboard
# 同一进程：QQ Adapter + Shared ChatService + Observatory
# 或退出已有实例后只读查看持久历史：
uv run personal-agent web
# 更换本地端口：
uv run personal-agent qq --dashboard --dashboard-port 8766
```

启动后访问 [http://127.0.0.1:8765](http://127.0.0.1:8765)。固定监听本机，没有公网 host 参数。原 CLI、模型窗口、角色配置等仍来自既有环境配置；没有额外 Dashboard 配置文件。启用新参数需重启旧的 QQ 进程，不能在旧进程旁再启动同库 Owner。

独立 web 使用相同 `open_application(..., observer_only=True)` 装配，取得原数据库锁，不创建聊天、维护或模型请求；跳过 Memory 发布、Learning 恢复与角色状态初始化。空目录展示空态或 unavailable，不自动修复。schema 初始化与会话选择仍属于应用启动阶段。HTTP 只读且不负责恢复历史工具。

## 交付提交

实际起点：`6dba693008c2cdeacc9217840c5326a63d6de0d0`。用户提供的参考 `9105269` 是此前的 QQ 持久去重提交；实现使用实际 HEAD。

| 里程碑 | 提交 | 实际内容 |
| --- | --- | --- |
| M1 | `07469c6` | FastAPI、Owner 连接上的 SELECT、读取边界、web / qq --dashboard 生命周期 |
| M2 | `4b9a783` | Neo-brutalist Bento、响应式设计、基础卡片和 DOM 组件 |
| M3 | `11edd52` | Thread → Episode → Run、Memory → Learning 回执、P3、Drawer 和来源 |
| M4 | `5b23ee9` | 真实 Stage / QQ 状态、只读诊断、有界时间线与轮询 |
| M5 | 本文所在的 `feat: 完成 Observatory 集成验收与只读启动保护` 提交 | 只读启动保护、最终集成/边界测试、安装包验证、截图和文档 |

开发过程中保留了另一个 QQ 工作提交 `7c42d78`；未重置、改写或合并其历史。最终 SHA 以交付消息与 `git log -1` 为准，避免本文引用自身提交哈希。

## 组件与交互

详细职责、实际执行步骤、异常分支与测试定位见 [模块文档](modules/dashboard.md)。前端为 Jinja2 + 原生 ES Modules + CSS Grid，不依赖 CDN、React、HTMX 下载或前端 Build Pipeline。布局元数据 `layout.py:CARDS` 与卡片模板独立，不引入拖拽框架。

首页包含 Agent Status、Activity、Character、Threads、Memory、Background Jobs、Recent Runs。Thread Inspector 展示 Metadata、Current State 与嵌套 Episodes；Episode 展示 Summary、Highlights、来源 Run；Run 初次只读元数据，Reveal 后再加载真实消息/工具记录，每条消息默认折叠。Memory 文件关联真实 Learning operations，Character 区分稳定定义、有效短期状态与来源回执。

同一个原生 dialog 提供焦点限制、关闭和 ESC；每层节点进入 URL fragment/Breadcrumb，浏览器返回、刷新、完整宽度详情均保留路径。子按钮阻止冒泡，详情异步请求有版本检查；后台刷新不改写 Inspector。长正文和工具记录有滚动区域，不撑开首页。

## 实际数据源与 API

所有 SQL 都在固定只读 Repository 中，绑定当前 Agent，不接收 Agent ID 参数。列表限制 limit，使用 cursor，排序稳定；HTTP 不调用 Agent、LLM、Repair、Retry 或 Host 管理操作。

| GET 接口 | 数据来源 / 读取范围 |
| --- | --- |
| `/api/status` | 同一个 ChatService 前台标记、OrganizationRuntime 只读诊断、真实 QQ 生命周期、最新 Run |
| `/api/runs`、`/{id}`、`/{id}/messages` | SQLite runs/conversations/messages；元数据与正文分离；可用 episode_id 过滤 |
| `/api/episodes`、`/{id}` | EpisodeRepository 校验后的 payload、episode_runs；thread_id 过滤重新校验归属 |
| `/api/threads`、`/{id}` | ThreadRepository 校验的 topic_threads，保留 revision/dirty/source_revision |
| `/api/memory`、`/file`、`/records` | 同一 Agent 的 MemoryFS 路径防护、单目录/文件分段、Learning 操作路径回查 |
| `/api/learning/jobs`、`/{id}` | 既有 LearningRepository 数据：sources、operations、hash、状态、回执和 metrics |
| `/api/character`、`/state` | 已加载 Package、CharacterStateService.active() TTL、当前存储状态、source-attempt receipts |
| `/api/activity` | 真实持久事实 + 最近内存 Stage 观测；最多每类 64 条、内存最多 128 条 |

列表按 rowid 倒序表示最新创建，而非宣称拥有每个对象的完整更新时间。正文/结果预览有界，原 SQLite 数据不变。文件后续片段校验 expected_hash；变化后需重新打开。

状态每 3 秒更新，其他概览每五轮更新；隐藏页面暂停，无重叠轮询。Memory 不递归扫描、不随状态轮询读取。SQLite 查询保留 VM 工作预算，不承诺同步磁盘的硬取消。

时间线的 Run 起止、Learning/Curator 回执更新时间和当前 Character 更新时间来自持久记录；Stage 事件来自现有 `_execute` 的 await 边界。Episode/Thread 只有源时间，因此显示 snapshot 并标明时间语义。历史 Stage 事件不伪造恢复，`scope_limited` 明确它只是近期窗口。

## 安全与隐私

固定 loopback，校验 Host/Origin/Sec-Fetch-Site，拒绝跨站与写方法，不提供数据库或任意文件下载、CORS 或公网参数。无缓存、严格 CSP、no-referrer、nosniff。原始文本通过 textContent 显示；Markdown 禁用 HTML、图片和链接，客户端再复制无属性标签白名单。明显凭据通过模式/字段脱敏，不输出配置、OpenID、Token、opaque Provider JSON 或 reasoning。脱敏不等于完整 DLP。

Memory 路径复用已有穿越、Symlink/Junction、Hardlink 防护；Dashboard 没有 Markdown、Character 或 Core 编辑权限。HTTP 异常转成固定错误，日志不打印正文/SDK 异常。QQ 集成测试验证即使只读查询失败，原 consumer 仍能完成回复。

## 验证记录

全部自动化使用临时 SQLite、MemoryFS、FakeProvider 或 MockTransport，并保留测试网络保护；未执行付费模型测试。

- M1 完整回归：752 passed、2 skipped。
- M3 关系/Memory/P3 定向：26 passed。
- M4 Runtime/QQ/Organization 定向：52 passed。
- M5 最终完整回归：768 passed、2 skipped（172.88 秒）；包含全类型隔离、硬链接/分段、只读重启和混合时区排序回归。
- `uv run ruff check .`、`uv run ruff format --check .` 通过；原生 JavaScript `node --check` 通过。
- `uv build --offline` 成功，实际将 Wheel 安装到独立目录，用 Python `-I` 优先加载安装包而非源码；首页、状态及 6 个 CSS/JS 路径共 8 个请求通过。
- CLI `personal-agent --help` 确认 web、--dashboard 和端口参数；生命周期测试使用实际本机 HTTP，验证端口冲突、同一个 ChatService、无第二个 Owner、查询失败隔离和退出后停止监听。
- Edge CDP 实际浏览器验收通过：Thread → Episode → Run、消息 Reveal、Breadcrumb、ESC、轮询保持 DOM/URL、Markdown XSS 防护、全宽切换、刷新恢复，以及 1440 px / 390 px 无水平溢出。未捕获 JavaScript 异常。

验证使用仓库忽略目录中的隔离虚拟环境，避免占用中的 `personal-agent.exe` 文件锁。Windows 沙箱的 socketpair 限制通过沙箱外运行离线测试解决，没有移除网络禁用 fixture。

## 视觉产物

以下全部为 **FakeProvider + 隔离合成数据**，不是用户私人聊天，也不代表真实 QQ 连接。QA 服务曾在本机 8766 验证，交付时关闭；实际使用请按上方命令启动。

![桌面工作台：合成数据](images/observatory-desktop-synthetic.png)

![Memory 与 Learning 回执：合成数据](images/observatory-memory-inspector-synthetic.png)

[Run Inspector 截图](images/observatory-run-inspector-synthetic.png) · [390 px 移动端截图](images/observatory-mobile-synthetic.png)

## 手动验收流程

1. 重启 QQ 为 `qq --dashboard`，打开本机 URL，确认 QQ Gateway 显示真实连接状态；初次无历史时为空态。
2. 发一条 QQ 消息，查看前台状态和新 Run，点击 Reveal 查看输入/响应/工具 call_id；没有工具时不伪造工具记录。
3. 等待原有 idle/累计数量触发，确认 Stage、Episode、Thread 和 Learning 回执按已有策略产生；Dashboard 不改变维护预算或主动强制执行。
4. 从 Thread 进入 Episode、Run，再用 Breadcrumb/ESC 返回；刷新详情 URL、切换全宽，等待轮询，确认详情不被重置。
5. 从 Memory 文件进入关联 Job，核对路径、Hash、来源 Episode/Run；角色关闭或状态 TTL 到期时检查对应空态。
6. 退出应用，确认 HTTP 关闭；重启只读 web 查看持久历史。真实 QQ/模型流程需用户自行运行，未以付费调用作为本轮自动验证。

## 限制与明确未实现

单用户本机只读；没有认证、远程部署、Web Chat、编辑器、Repair/Retry、主动消息、自由拖拽或主题系统。独立 web 不与已运行 QQ 并行争夺同库，须重启 QQ 启用同进程 Dashboard。

事件为近期窗口，内存 Stage 事件重启丢失；只有当前 Character 状态正文和来源处理回执，没有完整状态演变正文或 Memory 版本历史。没有可靠旧正文就不显示 Diff。消息、工具结果和文件按预算分页/预览，可能截断；完整审计仍以 SQLite/Markdown 为准。独立 web 遇到遗留 running 记录显示 Unknown，不假装它是当前前台运行。

## 界面语言

默认中文，页头和详情抽屉均可切换 English / 中文，浏览器记住选择。切换仅影响界面文案，不翻译来源正文或改变当前详情。中文界面合成数据示例见 [截图](images/observatory-chinese-synthetic.png)。

语言改动验证：完整离线回归 769 passed、2 skipped；Ruff 检查/格式检查与 `uv build --offline` 通过；实际 Wheel 安装的 10 个 HTML/API/资源请求通过，包含新增词典和语言模块。Edge 验证默认中文、英文切换、刷新恢复、抽屉内切换、存储拒绝、正文/详情状态保持及移动端无溢出，未捕获 JavaScript 异常。截图使用隔离合成数据，未调用真实模型。

## 栏目导航与按钮布局更新

顶部总览、主题线索、记忆、运行记录改为独立页面，地址分别为 `/`、`/threads`、`/memory`、`/runs`，不再向下滚动定位。专页显示对应内容并支持分页，详情仍用原 Drawer，关闭后保留所属页面。语言按钮移到连接状态与本机标记右侧，使用紧凑浅蓝色样式和浅紫色悬停反馈。

[独立主题页截图（合成数据）](images/observatory-pages-synthetic.png)。真实 Edge 检查四页导航、当前页高亮、语言保持、直接刷新/后退、详情关闭位置、按钮顺序/配色及 390 px 无水平溢出均通过，未捕获 JavaScript 异常。

栏目更新验证：完整离线回归 770 passed、2 skipped；Ruff 检查与格式检查、离线构建通过。实际安装 Wheel 的四页与相关资源共 13 个请求通过；浏览器导航与移动布局验证通过，无真实模型调用。

## 一屏总览与首屏语言更新

总览仅保留智能体状态、活动、角色、后台任务；主题、记忆和运行列表进入独立栏目查看。工作台按窗口高度自适应，缩小标题/间距并调整预览数量，更多数据使用卡片内滚动、分页或详情，不再要求整页下滑。

语言切换同步保存语言 cookie，页面由服务端直接输出选定语言；头部的同源阻塞脚本兼容旧 localStorage 偏好，避免先中文后英文。没有放宽 CSP，也没有增加宿主写接口。

[桌面一屏截图（合成数据）](images/observatory-fit-desktop-synthetic.png) · [手机一屏截图（合成数据）](images/observatory-fit-mobile-synthetic.png)。Edge 完成七种窗口尺寸 × 四个页面共 28 组文档溢出/卡片可用高度检查；延迟/阻止翻译模块时的首屏语言检查通过，无 JavaScript 异常。

本轮最终验证：完整离线回归 771 passed、2 skipped；Ruff 检查/格式检查、离线构建通过。最终 Wheel 安装后的四页及资源共 15 个请求通过；七种视口 × 四页和翻译脚本加载前的语言验证通过。截图全部使用隔离合成数据，无真实模型调用。

## 记忆阅读与图标更新

总览模板兼容旧运行进程传入的七卡 context，只渲染状态、活动、角色、后台任务，避免新 CSS 将旧卡片压缩。标题后的白框原为描边句点装饰，现已移除。记忆栏目默认打开总索引，左侧导航基础文件/主题目录，中间阅读正文与来源，右侧查看真实学习任务；不以装饰性统计填补没有数据的空白。

图标采用 [Lucide](https://lucide.dev/) 本地 SVG 子集，许可随包附带于 `static/ICONS-LICENSE.txt`，来源为[官方许可证](https://lucide.dev/license)。无需外部请求或额外运行时依赖。浏览器已验证图标、文件阅读、来源抽屉、恶意 Markdown 防护和响应式布局。

[总览图标截图（合成数据）](images/observatory-icons-synthetic.png) · [记忆阅读截图（合成数据）](images/observatory-memory-reader-synthetic.png)。

本轮验证：完整离线回归 784 passed、2 skipped；Ruff 检查/格式检查与离线构建通过；实际安装 Wheel 的四页与图标/语言/记忆阅读资源共 18 个请求通过。当前本机服务只读取 HTML 卡片 ID，确认旧上下文已仅渲染四卡。Edge 的记忆阅读、安全展示和图标/多视口检查通过；所有截图均为隔离合成数据，未执行付费模型调用。

## 对话消息与逐轮 Prompt

运行详情自动展示用户、助手和工具消息的折叠标题，正文逐条点击打开，移除原隐私说明与总展开按钮。原 `#29` 一类数字是 Conversation 内的存储序号，现作为次要标注，避免与模型轮次混淆。hb 标记上的星号为装饰，已移除。

新增“模型轮次与 Prompt”完整详情入口。schema 14 在原 Owner 内为后续主 Agent 调用保存当时的内部消息/工具定义、返回、实际 token 用量、耗时和失败状态，不保存 opaque Provider/reasoning 数据，不参与模型重放。旧 Run 缺失时显示空态，不用当前 Memory 推测旧 Prompt；快照最多 2 MiB，网页按需脱敏分页。详细执行和异常边界见[审计模块](modules/tracing.md)。重启正在运行的服务后才启用新迁移、接口和记录器。

[消息列表（合成数据）](images/observatory-messages-synthetic.png) · [模型轮次（合成数据）](images/observatory-model-rounds-synthetic.png)。

本轮验证：完整离线回归 803 passed、2 skipped；Ruff 检查/格式检查、`uv build --offline` 与 CLI `--help` 通过。构建遇到系统 C 盘空间不足后改用工作区 D 盘临时目录完成，不清理用户文件。隔离安装 Wheel 的 19 个页面/API/资源请求及 schema 14 检查通过。Edge 验证默认消息折叠、旧按钮/说明删除、轮次完整视图、输入/返回按需读取、Prompt 分页、第二轮工具证据、语言切换保持正文、刷新、桌面/手机宽度和旧 Run 空态，无 JavaScript 异常。仅使用合成数据和 FakeProvider，未调用付费模型。

## 时间可读性与详情布局

详情时间改为本地年月日、24 小时制时分秒和明确时区偏移。`2026-10-28T14:00:21.301813+00:00` 表示 UTC 的 14:00:21，在上海显示 `2026年10月28日 22:00:21 (北京时间 UTC+08:00)`。中英文切换更新显示，原始时间和审计正文保持不变。

运行详情的入口及轮次视图标题统一改为“模型轮次”。入口固定在右下角，正文区域独立滚动；其他对象不显示此按钮。删除原“来源数据 / 只读、完整详情”整行，轮次仍直接进入完整视图。

本轮离线回归 803 passed、2 skipped，Ruff 检查/格式检查与离线构建通过。合成 Edge 验证上海与纽约时区的示例换算、中英文日期更新、消息滚动后按钮可见、底栏删除、模型轮次入口/返回/分页及桌面和手机横竖屏布局，无 JavaScript 异常。上方消息和轮次截图已更新为合成数据的新布局。

## 悬停边框与简洁时间

运行等卡片悬停时向左上方移动，原列表左侧没有留白，导致第一列边框进入滚动容器的裁切区。列表内增加 10 px 空间，容纳悬停位移、阴影和焦点框，保留原交互。详情时间继续转换为浏览器本地时间，但删除括号内的时区说明，只显示日期与时分秒。页头品牌标记改为大写 `HB`。

[悬停效果（合成数据）](images/observatory-run-hover-synthetic.png)。完整离线回归 811 passed、2 skipped；Ruff 检查/格式检查与离线构建通过。Edge 以真实鼠标悬停及焦点操作检查 1440×900、1024×600、390×844、844×390 的边框/页面边界，同时验证大写标记、中英文时间和轮次页无时区后缀，无 JavaScript 异常。

## 品牌与栏目标题精简

品牌区删除“观测台”副标题，使 HB 与 HeyBuddy 使用一致字号并居中对齐。总览主标题改为“总览”，四页标题均复用对应导航图标。删除个人智能体/运行笔记、介绍文案、星形装饰、展开你的智能体、只读工作台及底部右侧访问说明；保留顶部导航、连接状态和语言切换。

页面标题直接取当前导航名称，兼容旧服务传入的旧总览 heading。CSS 清理已删除内容的规则，窄屏和中等宽度收紧品牌/控件间距，英文导航也保持可见。短窗口继续显示栏目标题，卡片使用剩余高度。

[桌面（合成数据）](images/observatory-clean-overview-synthetic.png) · [手机（合成数据）](images/observatory-clean-mobile-synthetic.png)。完整离线回归 812 passed、2 skipped，Ruff 检查/格式检查、离线构建和隔离安装 Wheel 的 19 个页面/API/资源请求通过。禁用浏览器扩展的独立 Edge 完成四页 × 七种视口 × 两种语言共 56 组检查，验证标题/图标、品牌对齐、文案删除、页头边界和卡片可用高度，无页面脚本异常。验证仅用合成数据，未调用真实模型。
