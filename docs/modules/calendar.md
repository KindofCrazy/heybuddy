# 有限中文时间语法与提醒意图

## 职责与架构组成

`core/calendar.py` 是无存储、无模型调用的时间解析模块。`TimeParts` 保存已识别日期、原始小时、分钟、时段、24 小时制标记、相对时长结果和未消费正文；`parse_time()` 不创建任务，`clock_hour()` 不猜缺失的上午/下午。`number()` 仅转换语法支持的数字，不提供通用中文数字分析。

`core/reminder_intent.py::reminder_action()` 是当前用户文本的保守提醒门控，返回 create/list/cancel_latest/cancel_ambiguous/abandon 或 None。提醒服务使用这个结果；QQ 聚合也将明确提醒视为边界。计划观察器只复用时间解析，不把时间表达式当成提醒授权。

## 代码实际运行路径

1. CLI 或 QQ 的 Host 提醒入口把当前文本和带时区收件时间交给 `scheduler/natural.py::respond()`；门控先排除过长文本、斜杠命令、引用和明显的假设或元讨论。
2. `parse_time()` 默认将参考时间转为 UTC+8；社交窗口显式传入 ProactivePolicy 的固定时区。依次提取带时区 ISO8601、相对时长、日期、上午/下午等时段和时刻，每类至多一个。提取后的正文返回调用方，不执行任何写入。
3. 日期支持今天/明天/后天/大后天、周/星期、本周/这周/下周、数字月日和 YYYY-MM-DD。时刻支持“九点半”“三点一刻”“下午三点二十分”和 09:15；时长支持数字或简单中文数字的秒、分钟、小时、天及“半小时后”。不支持的语法不是成功解析的证明。
4. `clock_hour()` 对 1～12 点且没有时段的中文时刻返回 None，提醒服务据此追问；明确 24 小时制或大于十二的小时可直接计算。日期没有被要求时使用原请求当天，过去时刻由 Repository 拒绝，绝不自动推迟到次日。
5. 提醒服务合并同来源草稿中实际收到的补充字段，最后仍由 ReminderRequest 与 Repository 校验；`proactive/timing.py::followup_window()` 则将已验证计划的日期用于社交窗口，绝不调用 Scheduler。

## 异常与边界

非法日期、多个同类时间、混合相对/绝对时间、超范围小时或分钟返回 error=True，不输出异常输入。引文、假设和否定的保守门控不能替代完整语义理解；复杂表达应重新明确或使用 /remind。任务正文里的“第九点”不会当成九点钟。

当前采用固定偏移，没有地理时区数据库、夏令时推断或多语言日历。解析结果不授予 Memory、文件、工具或真实发送权限。

## 阅读与验证

最小使用是调用 `parse_time("明天上午九点", aware_received_at)` 查看 TimeParts，再用 `clock_hour()` 检查时段。对应的离线行为用例位于 `tests/test_reminder_natural.py` 与 `tests/test_social_timing.py`；2026-10-31 用户已允许运行测试，仍不进行真实模型验证。
