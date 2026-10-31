# 中文提醒与关心式跟进交付

2026-10-31 本轮实现有限中文提醒与来源隔离的缺项草稿；明确提醒复用独立 Scheduler。普通未来计划仍进入 Proactive follow_up，有日期时采用稳定晚间/事前窗口，默认只生成 Shadow 草稿。真实 NapCat Sender 只接受当前渠道来源、会话和主号的明确提醒，不开放 Proactive 真实发送或任意 OneBot 操作。

## 已执行的验证

- 提醒、NapCat、Proactive、Outbox 与提示词快照相关回归：181 passed。
- 最后时间边界复核：25 passed；完整 `uv run pytest -q`：1262 passed、3 skipped。测试保留 `tests/conftest.py` 的外网保护，Fake Provider / 假 WS 回执，无真实模型费用。
- 本次修改的 Python 文件 Ruff check 与 format check 通过；从 HEAD 加本次暂存改动导出的独立审阅树也通过全量 Ruff，325 files already formatted。原工作区仍有并行 Organization 修改中的导入排序/格式问题，未改写或打包进提交。
- `uv build` 成功生成源码包与 wheel，CLI `personal-agent --help` 退出正常。初次沙箱构建无法获取 hatchling，随后在获准环境完成隔离构建，并使用缓存离线重建。
- Windows 沙箱阻止默认临时目录与 asyncio loopback socketpair，改用工作区隔离 basetemp 并在获准环境执行；未取消测试的禁外网 Fixture。

## 真实投递状态

用户明确授权并完成登录确认后，使用隔离 SQLite、现有 NapCat 绑定和固定“HeyBuddy 提醒投递测试”文本完成真实验证。安排三秒后的一次性提醒，实际结果为 status=accepted、mode=napcat，取得 OneBot 数值 message_id，model_requests=0；没有装配 Provider、Memory 或后台维护。只发送这一条固定测试消息，之前登录尚未就绪的探针在创建提醒前就停止。

真实回执仅证明渠道接受，不宣称用户已读。发送结果不确定时不重发。探针的数据库、账号、实际回执号和日志保留在 Git 忽略目录，不提交私人数据或凭据。随后使用既有 `scripts/start-qq.ps1 -Restart -NoBrowser` 正常重启 HeyBuddy，启动脚本报告 ready，更新后的运行服务已加载中文提醒与明确提醒 Sender。

## 最小使用与限制

在更新后的 CLI 或 QQ/NapCat 中说“明天上午九点提醒我交材料”或“十分钟后提醒我喝水”；缺项会先追问。“九点”没有时段时不猜，“取消刚才那个提醒”只处理当前来源最近的记录。也可继续使用 /remind list/show/cancel。

真实到期提醒需要更新后的 NapCat 服务保持在线、身份核对完成且提醒开关开启。断线或停机之前已经到期的记录不会在重连集中补发。普通未来计划不会进入提醒队列；社交随机时间也不代表必须发消息，仍受安静时段、最新用户证据、Attention 与预算控制。

具体执行路径见 [Scheduler](modules/scheduler.md)、[时间语法](modules/calendar.md)、[Proactive](modules/proactive.md) 与 [NapCat](modules/napcat.md)。
