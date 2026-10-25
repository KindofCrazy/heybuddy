import sqlite3

import pytest

from personal_agent.core.history import HistoryError
from personal_agent.core.models import (
    AssistantMessage,
    RunResult,
    TextMessage,
    ToolMessage,
    ToolResult,
)
from personal_agent.storage import sqlite as storage
from personal_agent.storage.sqlite import SQLiteStore
from tests.fakes import add_call


@pytest.fixture
def store(tmp_path):
    with SQLiteStore(tmp_path / "test.sqlite3") as store:
        store.initialize()
        yield store


def conversation(store):
    agent = store.create_agent("test", "test system")
    return store.create_conversation(agent.id)


def complete_turn(store, conversation_id, text="hello"):
    run_id = store.begin_run(conversation_id, TextMessage(role="user", text=text))
    store.append_message(run_id, AssistantMessage(text="answer"))
    store.finish_run(run_id, RunResult(status="completed", text="answer", rounds=1))
    return run_id


def test_initialization_is_explicit_versioned_and_idempotent(tmp_path):
    path = tmp_path / "nested" / "db.sqlite3"
    with SQLiteStore(path) as store:
        with pytest.raises(HistoryError):
            store.list_conversations("missing")
        store.initialize()
        conv = conversation(store)
        store.initialize()
        assert store.get_conversation(conv.id) == conv
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION


def test_migration_failure_rolls_back_schema_and_version(tmp_path, monkeypatch):
    path = tmp_path / "migration.sqlite3"
    original = storage.MIGRATIONS[1]
    with SQLiteStore(path) as store:
        monkeypatch.setitem(storage.MIGRATIONS, 1, (*original, "INVALID SQL"))
        with pytest.raises(HistoryError):
            store.initialize()
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 0
            assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
        monkeypatch.setitem(storage.MIGRATIONS, 1, original)
        store.initialize()
        assert conversation(store).id


def test_newer_database_is_not_downgraded(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=999")
    with SQLiteStore(path) as store, pytest.raises(HistoryError, match="newer"):
        store.initialize()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 999


def test_unrecognized_existing_database_is_preserved(tmp_path):
    path = tmp_path / "unrelated.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated (value TEXT)")
        db.execute("INSERT INTO unrelated VALUES ('preserve')")
    with SQLiteStore(path) as store, pytest.raises(HistoryError, match="unrecognized"):
        store.initialize()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM unrelated").fetchall() == [("preserve",)]


def test_single_owner_lock_released_on_close(store):
    with pytest.raises(HistoryError, match="in use"):
        SQLiteStore(store.path)
    path = store.path
    store.close()
    with SQLiteStore(path) as reopened:
        reopened.initialize()


def test_multiple_agents_and_conversations_are_separate(store):
    first = conversation(store)
    second = conversation(store)
    third = store.create_conversation(first.agent_id)
    complete_turn(store, first.id, "private first")
    complete_turn(store, second.id, "private second")
    assert {item.id for item in store.list_conversations(first.agent_id)} == {first.id, third.id}
    assert store.load_messages(third.id) == []
    assert store.load_messages(first.id)[0].content.text == "private first"
    assert store.load_messages(second.id)[0].content.text == "private second"
    with pytest.raises(HistoryError):
        store.select_conversation(first.agent_id, second.id)
    assert store.last_conversation(first.agent_id) is None


def test_message_order_ids_and_unicode_survive_multiple_reopens(tmp_path):
    path = tmp_path / "restarts.sqlite3"
    with SQLiteStore(path) as store:
        store.initialize()
        conv = conversation(store)
        run_id = complete_turn(store, conv.id, "我正在开发 HeyBuddy")
        original = store.load_messages(conv.id)
    for _ in range(3):
        with SQLiteStore(path) as store:
            store.initialize()
            assert store.recover_incomplete_runs() == 0
            assert store.load_messages(conv.id) == original
            assert [item.sequence for item in original] == [1, 2]
            run = store.list_runs(conv.id)[0]
            assert run.id == run_id
            assert run.status == "completed"
            assert run.ended_at is not None
            assert run.result.text == "answer"


def test_tool_pairing_and_provider_payload_are_lossless(store):
    conv = conversation(store)
    run_id = store.begin_run(conv.id, TextMessage(role="user", text="sum"))
    assistant = AssistantMessage(
        tool_calls=[add_call("a"), add_call("b")],
        provider_data={
            "openai": [
                {"type": "reasoning", "summary": [], "encrypted_content": "opaque-测试"},
                {
                    "type": "function_call",
                    "call_id": "a",
                    "name": "add_numbers",
                    "arguments": '{"a":2,"b":3}',
                },
            ],
            "other": [{"nested": {"values": [None, True, 12.5]}}],
        },
    )
    store.append_message(run_id, assistant)
    for call_id in ["a", "b"]:
        store.append_message(
            run_id,
            ToolMessage(
                result=ToolResult(call_id=call_id, name="add_numbers", ok=True, data={"sum": 5})
            ),
        )
    store.append_message(run_id, AssistantMessage(text="5 and 5"))
    store.finish_run(run_id, RunResult(status="completed", text="5 and 5", rounds=2))
    rows = store.load_messages(conv.id, replayable_only=True)
    assert rows[1].content == assistant
    assert [
        item.content.result.call_id for item in rows if isinstance(item.content, ToolMessage)
    ] == ["a", "b"]


def test_invalid_pairing_and_incomplete_finish_are_atomic(store):
    conv = conversation(store)
    run_id = store.begin_run(conv.id, TextMessage(role="user", text="sum"))
    store.append_message(run_id, AssistantMessage(tool_calls=[add_call()]))
    before = store.load_messages(conv.id)
    with pytest.raises(HistoryError, match="match"):
        store.append_message(
            run_id,
            ToolMessage(result=ToolResult(call_id="wrong", name="add_numbers", ok=True, data=5)),
        )
    with pytest.raises(HistoryError, match="incomplete"):
        store.finish_run(run_id, RunResult(status="completed", rounds=1))
    assert store.load_messages(conv.id) == before
    assert store.list_runs(conv.id)[0].status == "running"


def test_begin_run_and_user_message_rollback_together(store):
    conv = conversation(store)
    with sqlite3.connect(store.path) as db:
        db.execute(
            "CREATE TRIGGER fail_insert BEFORE INSERT ON messages "
            "BEGIN SELECT RAISE(ABORT,'private database detail'); END"
        )
    with pytest.raises(HistoryError) as caught:
        store.begin_run(conv.id, TextMessage(role="user", text="private user input"))
    assert "private" not in str(caught.value)
    assert store.list_runs(conv.id) == []
    assert store.load_messages(conv.id) == []


def test_finish_failure_does_not_publish_partial_completed_run(store):
    conv = conversation(store)
    run_id = store.begin_run(conv.id, TextMessage(role="user", text="user"))
    store.append_message(run_id, AssistantMessage(text="answer"))
    with sqlite3.connect(store.path) as db:
        db.execute(
            "CREATE TRIGGER fail_update BEFORE UPDATE ON conversations "
            "BEGIN SELECT RAISE(ABORT,'private detail'); END"
        )
    with pytest.raises(HistoryError):
        store.finish_run(run_id, RunResult(status="completed", rounds=1))
    assert store.list_runs(conv.id)[0].status == "running"
    assert store.load_messages(conv.id, replayable_only=True) == []
    assert len(store.load_messages(conv.id)) == 2


def test_restart_marks_unfinished_run_once_and_preserves_full_audit(tmp_path):
    path = tmp_path / "interrupted.sqlite3"
    with SQLiteStore(path) as store:
        store.initialize()
        conv = conversation(store)
        complete_turn(store, conv.id)
        run_id = store.begin_run(conv.id, TextMessage(role="user", text="interrupted input"))
        store.append_message(run_id, AssistantMessage(tool_calls=[add_call("pending")]))
        audit = store.load_messages(conv.id)
    with SQLiteStore(path) as store:
        store.initialize()
        assert store.recover_incomplete_runs() == 1
        assert store.recover_incomplete_runs() == 0
        assert store.list_runs(conv.id)[-1].status == "interrupted"
        assert store.load_messages(conv.id) == audit
        assert len(store.load_messages(conv.id, replayable_only=True)) == 2


def test_corrupt_message_is_reported_without_exposing_content(store):
    conv = conversation(store)
    complete_turn(store, conv.id)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE messages SET payload='private corrupt body'")
    with pytest.raises(HistoryError) as caught:
        store.load_messages(conv.id, replayable_only=True)
    assert "private" not in str(caught.value)


def test_closed_database_errors_are_sanitized(store):
    store.close()
    with pytest.raises(HistoryError, match="operation failed"):
        store.list_conversations("heybuddy")


def test_same_conversation_cannot_have_overlapping_running_runs(store):
    conv = conversation(store)
    first = store.begin_run(conv.id, TextMessage(role="user", text="first"))
    with pytest.raises(HistoryError):
        store.begin_run(conv.id, TextMessage(role="user", text="second"))
    assert [run.id for run in store.list_runs(conv.id)] == [first]
    assert len(store.load_messages(conv.id)) == 1


def test_invalid_database_file_fails_safely_and_releases_owner_lock(tmp_path):
    path = tmp_path / "invalid.sqlite3"
    path.write_bytes(b"private invalid database contents")
    for _ in range(2):
        with pytest.raises(HistoryError) as caught:
            with SQLiteStore(path) as store:
                store.initialize()
        assert "private" not in str(caught.value)
