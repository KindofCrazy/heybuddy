"""Host-owned local time context; never stored as a journal message."""

from datetime import UTC, datetime, tzinfo


def temporal_context(previous: datetime | None, timezone: tzinfo | None = None) -> dict:
    current = datetime.now(UTC).astimezone(timezone)
    return {
        "kind": "temporal_context",
        "trust": "host_clock_data",
        "current_time": current.isoformat(),
        "timezone": str(current.tzinfo),
        "previous_interaction_at": previous.astimezone(current.tzinfo).isoformat()
        if previous
        else None,
        "seconds_since_previous_interaction": max(0, int((current - previous).total_seconds()))
        if previous
        else None,
    }
