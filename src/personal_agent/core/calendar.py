"""Small explicit Chinese time grammar shared by reminders and social follow-ups."""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo

LOCAL_ZONE = timezone(timedelta(hours=8))
NUMBER = r"(?:[0-9]{1,6}|[零〇一二两三四五六七八九十百]{1,4})"
PERIOD = r"凌晨|早上|早晨|上午|中午|下午|傍晚|晚上"


def number(value: str) -> int:
    if value.isascii() and value.isdigit():
        return int(value)
    digits = {c: i for i, c in enumerate("零一二三四五六七八九")}
    digits.update(〇=0, 两=2)
    if value in digits:
        return digits[value]
    if "百" in value:
        raise ValueError("Unsupported number")
    left, separator, right = value.partition("十")
    if separator:
        return (digits[left] if left else 1) * 10 + (digits[right] if right else 0)
    raise ValueError("Unsupported number")


@dataclass
class TimeParts:
    day: date | None = None
    hour: int | None = None
    minute: int = 0
    period: str | None = None
    clock_24h: bool = False
    due_at: datetime | None = None
    remainder: str = ""
    error: bool = False


def clock_hour(hour: int, period: str | None, clock_24h: bool = False) -> int | None:
    if not 0 <= hour <= 23:
        raise ValueError("Invalid hour")
    if period is None:
        return hour if clock_24h or hour == 0 or hour > 12 else None
    if period in {"下午", "傍晚", "晚上"}:
        if 1 <= hour <= 11:
            hour += 12
        if hour < 12 or (period == "晚上" and hour == 12):
            raise ValueError("Ambiguous period")
    elif period == "中午":
        if hour == 1:
            hour = 13
        if hour not in {11, 12, 13}:
            raise ValueError("Invalid noon hour")
    elif hour >= 12:
        raise ValueError("Invalid morning hour")
    return hour


def parse_time(text: str, reference: datetime, zone: tzinfo = LOCAL_ZONE) -> TimeParts:
    """Return consumed fields, never roll a past clock forward or invent a missing clock."""
    reference = reference.astimezone(zone)
    text = text.replace("明晚", "明天晚上").replace("今晚", "今天晚上")
    result = TimeParts(remainder=text)

    def take(pattern: str):
        matches = list(re.finditer(pattern, result.remainder))
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("Multiple time expressions")
        match = matches[0]
        result.remainder = result.remainder[: match.start()] + result.remainder[match.end() :]
        return match

    try:
        iso = take(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})")
        if iso:
            result.due_at = datetime.fromisoformat(iso[0])
        duration = take(
            rf"(?:(?P<n>{NUMBER})(?P<u>秒钟?|分钟?|小时|天)|(?P<half>半小时))"
            r"(?:以后|之后|后)"
        )
        if duration:
            if result.due_at:
                raise ValueError("Mixed time expressions")
            amount = (
                1800
                if duration["half"]
                else number(duration["n"])
                * (
                    3600
                    if duration["u"] == "小时"
                    else 86400
                    if duration["u"] == "天"
                    else 60
                    if duration["u"].startswith("分")
                    else 1
                )
            )
            if amount <= 0:
                raise ValueError("Invalid duration")
            result.due_at = reference + timedelta(seconds=amount)
        day = take(
            r"(?:(?P<y>\d{4})年)?(?P<mo>\d{1,2})月(?P<d>\d{1,2})[日号]"
            r"|(?P<iso>\d{4}-\d{2}-\d{2})|(?P<relative>大后天|后天|明天|今天)"
            r"|(?P<week>下周|下星期|本周|这周|周|星期)(?P<weekday>[一二三四五六日天])"
        )
        if day:
            if day["iso"]:
                result.day = date.fromisoformat(day["iso"])
            elif day["mo"]:
                result.day = date(int(day["y"] or reference.year), int(day["mo"]), int(day["d"]))
            elif day["relative"]:
                offset = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}[day["relative"]]
                result.day = reference.date() + timedelta(days=offset)
            else:
                weekday = "一二三四五六日".index(day["weekday"].replace("天", "日"))
                week = day["week"]
                offset = weekday - reference.weekday()
                if week.startswith("下"):
                    offset += 7
                elif week in {"周", "星期"}:
                    offset %= 7
                result.day = reference.date() + timedelta(days=offset)
        period = take(PERIOD)
        if period:
            result.period = period[0]
        clock = take(
            rf"(?<![第0-9零〇一二两三四五六七八九十])(?P<h>{NUMBER})[点时]"
            rf"(?:(?P<special>半|一刻|三刻)|(?P<m>{NUMBER})分?)?"
            r"|(?P<colon_h>\d{1,2}):(?P<colon_m>\d{2})"
        )
        if clock:
            result.clock_24h = clock["colon_h"] is not None
            result.hour = number(clock["colon_h"] or clock["h"])
            result.minute = (
                {"半": 30, "一刻": 15, "三刻": 45}[clock["special"]]
                if clock["special"]
                else number(clock["colon_m"] or clock["m"] or "0")
            )
            if result.minute > 59:
                raise ValueError("Invalid minute")
            clock_hour(result.hour, result.period, result.clock_24h)
        if result.due_at and (result.day or result.hour is not None or result.period):
            raise ValueError("Mixed time expressions")
    except (ValueError, OverflowError):
        result.error = True
    return result
