import json
from datetime import UTC, datetime, timedelta, timezone

from personal_agent.core.temporal import temporal_context
from personal_agent.sessions import SessionManager
from personal_agent.storage.sqlite import SQLiteStore


def test_clock_uses_explicit_local_offset_across_midnight():
    zone = timezone(timedelta(hours=8))
    previous = datetime.now(UTC) - timedelta(minutes=1)
    data = temporal_context(previous, zone)
    assert data["current_time"].endswith("+08:00")
    assert 60 <= data["seconds_since_previous_interaction"] < 65


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
