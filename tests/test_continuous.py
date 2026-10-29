import json
from datetime import UTC, datetime, timedelta, timezone

from personal_agent.core.temporal import temporal_context
from personal_agent.sessions import SessionManager
from personal_agent.storage.sqlite import SQLiteStore


def test_clock_formats_local_time_without_zone_across_midnight(monkeypatch):
    zone = timezone(timedelta(hours=8))
    current = datetime(2026, 10, 28, 16, 0, 15, tzinfo=UTC)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current.astimezone(tz)

    monkeypatch.setattr("personal_agent.core.temporal.datetime", FixedDatetime)
    previous = datetime(2026, 10, 28, 15, 59, 45, tzinfo=UTC)
    data = temporal_context(previous, zone)
    assert data["current_time"] == "2026-10-29 00:00:15"
    assert data["previous_interaction_at"] == "2026-10-28 23:59:45"
    assert data["seconds_since_previous_interaction"] == 30
    assert "timezone" not in data


def test_stream_selection_preserves_legacy_conversations(tmp_path):
    with SQLiteStore(tmp_path / "streams.db") as store:
        store.initialize()
        sessions = SessionManager(store)
        original = sessions.current.id
        sessions.new()
        selected = sessions.current.id
        restored = SessionManager(store)
        assert restored.current.id == selected
        assert {c.id for c in restored.list()} == {original, selected}
        context = restored.context()
        messages = context.begin("hello")
        data = json.loads(context.for_request(messages)[1].text)
        assert data["kind"] == "temporal_context"
        assert data["previous_interaction_at"] is None
        context.abort()
        assert len(store.load_messages(selected)) == 1
