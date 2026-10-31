"""Persistable social windows from verified dated user evidence; never exact reminders."""

import hashlib
from datetime import datetime, time, timedelta, timezone

from personal_agent.core.calendar import clock_hour, parse_time
from personal_agent.proactive.models import ProactivePolicy


def _sample(seed: str, purpose: str) -> float:
    raw = hashlib.sha256((seed + ":" + purpose).encode()).digest()
    return int.from_bytes(raw[:8]) / 2**64


def followup_window(
    quote: str, source_at: datetime, seed: str, policy: ProactivePolicy
) -> dict | None:
    if not policy.social_followup_enabled:
        return None
    zone = timezone(timedelta(minutes=policy.utc_offset_minutes))
    parts = parse_time(quote, source_at, zone)
    if parts.error or (parts.day is None and parts.due_at is None and parts.hour is None):
        return None
    local = source_at.astimezone(zone)
    event = parts.due_at.astimezone(zone) if parts.due_at else None
    if event is None and parts.hour is not None:
        try:
            hour = clock_hour(parts.hour, parts.period, parts.clock_24h)
        except ValueError:
            hour = None
        if hour is not None:
            event = datetime.combine(parts.day or local.date(), time(hour, parts.minute), zone)
    important = any(word in quote for word in ("重要", "关键", "答辩", "面试", "考试", "手术"))
    before = (
        event is not None
        and event - source_at > timedelta(minutes=policy.before_max_minutes)
        and (important or _sample(seed, "before") < policy.before_probability)
    )
    if before:
        start = event - timedelta(minutes=policy.before_max_minutes)
        end = event - timedelta(minutes=policy.before_min_minutes)
        phase = "before"
    else:
        day = event.date() if event else parts.day
        if day is None:
            return None  # An ambiguous clock alone is not a dated evening opportunity.
        start = datetime.combine(day, time(), zone) + timedelta(minutes=policy.evening_start_minute)
        end = datetime.combine(day, time(), zone) + timedelta(minutes=policy.evening_end_minute)
        if event is not None:
            start = max(start, event + timedelta(minutes=30))
            if start >= end:
                start = datetime.combine(day + timedelta(days=1), time(), zone) + timedelta(
                    minutes=policy.evening_start_minute
                )
                end += timedelta(days=1)
        phase = "evening"
    # One stable sample per topic/source, with second precision and no restart re-roll.
    span = int((end - start).total_seconds())
    offset = 1 + int(_sample(seed, "offset") * max(1, span - 2))
    review = start + timedelta(seconds=offset)
    if review.second == 0:
        review += timedelta(seconds=1)
    return dict(
        phase=phase,
        review_at=review.isoformat(),
        start=start.isoformat(),
        end=end.isoformat(),
        event_at=event.isoformat() if event else None,
        important=important,
        mode="shadow",
    )
