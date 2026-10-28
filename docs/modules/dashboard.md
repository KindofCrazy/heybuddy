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

对象不存在或不属于当前 Agent 返回 404；参数错误返回固定 422；存储失败返回固定 503，不返回异常正文。只允许 GET/HEAD，禁用 API 文档写入口、跨站请求和不匹配 Host。所有页面禁止缓存，设置 CSP、no-referrer、nosniff；无远程资源和 CORS。监听端口冲突在启动 consumer 前失败，运行时 HTTP 异常不传播到 QQ task。

## 阅读与验证

`uv run personal-agent qq --dashboard` 或（先退出其他本地实例）`uv run personal-agent web`，访问 `http://127.0.0.1:8765`。独立 web 仍使用既有 Owner 锁，已有实例运行时明确拒绝，不开第二个写实例。`tests/test_dashboard.py` 使用临时 SQLite、MemoryFS、FakeProvider 和 ASGITransport，验证分页、隔离、只读和安全请求边界。无需真实 Key 或 API 请求。
