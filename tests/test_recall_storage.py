import json
import sqlite3

import pytest

from personal_agent.core.history import HistoryError
from personal_agent.core.models import (
    AssistantMessage,
    RunResult,
    TextMessage,
    ToolCall,
    ToolMessage,
    ToolResult,
)
from personal_agent.recall.service import EXPAND_TEXT_BYTES, OUTPUT_BYTES, Recall, RecallError
from personal_agent.storage import sqlite as storage
from personal_agent.storage.recall import RECENT_LIMIT, SCAN_CHARS
from personal_agent.storage.sqlite import SQLiteStore


@pytest.fixture
def state(tmp_path):
    with SQLiteStore(tmp_path / "recall.sqlite3") as store:
        store.initialize()
        agent = store.create_agent("test", "base")
        conversation = store.create_conversation(agent.id)
        yield store, agent, conversation, Recall(store, agent.id)


def turn(
    store,
    conversation_id,
    user="历史决策：先实现Memory",
    answer="Memory preserves user preferences.",
    status="completed",
):
    run = store.begin_run(conversation_id, TextMessage(role="user", text=user))
    store.append_message(run, AssistantMessage(text=answer))
    if status in {"completed", "failed"}:
        result = RunResult(
            status="completed" if status == "completed" else "provider_error", text=answer, rounds=1
        )
        store.finish_run(run, result)
    elif status != "running":
        store.interrupt_run(run, cancelled=status == "cancelled")
    return run


def legacy(path, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 1)
        with SQLiteStore(path) as store:
            store.initialize()
            agent = store.create_agent("legacy", "base")
            conversation = store.create_conversation(agent.id)
            turn(store, conversation.id)
            turn(store, conversation.id, user="不完整Memory", status="interrupted")
            return agent.id, conversation.id, store.load_messages(conversation.id)


def test_v1_migration_backfills_without_changing_canonical_history(tmp_path, monkeypatch):
    path = tmp_path / "v1.sqlite3"
    agent_id, conversation_id, original = legacy(path, monkeypatch)
    with SQLiteStore(path) as store:
        store.initialize()
        assert store.load_messages(conversation_id) == original
        hits = Recall(store, agent_id).search("Memory").hits
        assert {hit.message_id for hit in hits} == {item.id for item in original[:2]}
        assert {hit.role for hit in hits} == {"user", "assistant"}
        store.initialize()
        assert Recall(store, agent_id).search("Memory").hits == hits
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION


def test_recall_migration_failure_rolls_back_index_and_version(tmp_path, monkeypatch):
    path = tmp_path / "v1-failure.sqlite3"
    _, conversation_id, original = legacy(path, monkeypatch)
    migration = storage.MIGRATIONS[2]
    monkeypatch.setitem(storage.MIGRATIONS, 2, (*migration, "INVALID SQL"))
    with SQLiteStore(path) as store:
        with pytest.raises(HistoryError):
            store.initialize()
        assert store.load_messages(conversation_id) == original
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'recall_%'"
        ).fetchall()


def test_new_messages_are_searchable_only_after_finalization(state):
    store, _, conversation, recall = state
    run = turn(store, conversation.id, status="running")
    assert recall.search("Memory").hits == []
    store.finish_run(run, RunResult(status="completed", text="done", rounds=1))
    hits = recall.search("Memory").hits
    records = store.load_messages(conversation.id)
    for hit in hits:
        record = next(item for item in records if item.id == hit.message_id)
        assert hit.sequence == record.sequence
        assert hit.created_at == record.created_at
        assert hit.conversation_id == conversation.id
        assert hit.run_id == run and hit.run_status == "completed"


@pytest.mark.parametrize(
    "query", ["先实现", "Memory", "memory", "实现Memory", "Memory计划", "历史", "先"]
)
def test_chinese_english_and_mixed_literal_search(state, query):
    store, _, conversation, recall = state
    turn(store, conversation.id, user="历史决策：先实现Memory计划，再做Recall。", answer="ack")
    result = recall.search(query)
    assert len(result.hits) == 1
    assert result.mode == ("recent_literal" if len(query) < 3 else "fts5_trigram")
    assert result.scope_limited == (len(query) < 3)


def test_cross_conversation_search_but_not_cross_agent(state):
    store, agent, first, recall = state
    second = store.create_conversation(agent.id)
    foreign_agent = store.create_agent("foreign", "base")
    foreign = store.create_conversation(foreign_agent.id)
    for conversation in [first, second, foreign]:
        turn(store, conversation.id, user="共享关键词 keyword", answer="end")
    hits = recall.search("keyword").hits
    assert {hit.conversation_id for hit in hits} == {first.id, second.id}
    foreign_id = store.load_messages(foreign.id)[0].id
    for message_id in [foreign_id, "missing"]:
        with pytest.raises(RecallError) as caught:
            recall.get(message_id)
        assert caught.value.code == "recall_not_found"
    assert len(Recall(store, foreign_agent.id).search("keyword").hits) == 1


def test_only_text_not_provider_reasoning_arguments_or_tool_results_is_indexed(state):
    store, _, conversation, recall = state
    run = store.begin_run(conversation.id, TextMessage(role="user", text="normal question"))
    store.append_message(
        run,
        AssistantMessage(
            text="visible assistant text",
            tool_calls=[ToolCall(id="call", name="test", arguments='{"value":"argumentsecret"}')],
            provider_data={
                "openai": [{"type": "reasoning", "encrypted_content": "reasoningsecret"}]
            },
        ),
    )
    store.append_message(
        run,
        ToolMessage(result=ToolResult(call_id="call", name="test", ok=True, data="resultsecret")),
    )
    store.append_message(run, AssistantMessage(text="finished"))
    store.finish_run(run, RunResult(status="completed", text="finished", rounds=2))
    for query in ["argumentsecret", "reasoningsecret", "resultsecret", "encrypted_content"]:
        assert not recall.search(query).hits
    hit = recall.search("visible assistant").hits[0]
    assert len(recall.get(hit.message_id, before=3, after=3).messages) == 3
    assert "secret" not in recall.get(hit.message_id).model_dump_json()


@pytest.mark.parametrize("status", ["completed", "failed", "running", "cancelled", "interrupted"])
def test_run_visibility_applies_to_search_and_get(state, status):
    store, _, conversation, recall = state
    turn(store, conversation.id, status=status)
    message_id = store.load_messages(conversation.id)[0].id
    if status in {"completed", "failed"}:
        assert len(recall.search("Memory").hits) == 2
        assert recall.get(message_id).messages[0].run_status == status
    else:
        assert recall.search("Memory").hits == []
        with pytest.raises(RecallError, match="accessible"):
            recall.get(message_id)


def test_restart_preserves_results_and_excludes_recovered_interruption(tmp_path):
    path = tmp_path / "restart.sqlite3"
    with SQLiteStore(path) as store:
        store.initialize()
        agent = store.create_agent("test", "base")
        conversation = store.create_conversation(agent.id)
        turn(store, conversation.id)
        original = Recall(store, agent.id).search("Memory")
        turn(store, conversation.id, user="pending Memory", status="running")
    for _ in range(3):
        with SQLiteStore(path) as store:
            store.initialize()
            store.recover_incomplete_runs()
            assert Recall(store, agent.id).search("Memory") == original


def test_expansion_is_ordered_bounded_and_skips_incomplete_runs(state):
    store, _, conversation, recall = state
    for index in range(7):
        turn(store, conversation.id, user=f"fact {index}", answer="long " + '你\n\x00"' * 3000)
        if index == 2:
            turn(store, conversation.id, user="incomplete private", status="cancelled")
    anchor = recall.search("fact 3").hits[0]
    result = recall.get(anchor.message_id, before=3, after=3)
    assert len(result.messages) == 7
    assert [item.sequence for item in result.messages] == sorted(
        item.sequence for item in result.messages
    )
    assert any(item.message_id == anchor.message_id for item in result.messages)
    assert result.truncated
    assert "incomplete private" not in result.model_dump_json()
    assert (
        sum(
            len(json.dumps(item.text, ensure_ascii=False)[1:-1].encode())
            for item in result.messages
        )
        <= EXPAND_TEXT_BYTES
    )
    assert len(result.model_dump_json().encode()) <= OUTPUT_BYTES


def test_search_limit_snippet_budget_and_stable_order(state):
    store, _, conversation, recall = state
    for _ in range(6):
        turn(store, conversation.id, user="prefix " * 1000 + "needle " + "你" * 1000, answer="ok")
    first = recall.search("needle", limit=2)
    assert len(first.hits) == 2 and first.has_more
    assert all("needle" in hit.snippet and hit.truncated for hit in first.hits)
    assert all(
        len(json.dumps(hit.snippet, ensure_ascii=False)[1:-1].encode()) <= 512 for hit in first.hits
    )
    assert len(first.model_dump_json().encode()) <= OUTPUT_BYTES
    assert recall.search("needle", limit=2) == first


@pytest.mark.parametrize("query", ["", "   ", "x" * 121, "bad\x00query", "line\nquery"])
def test_invalid_queries_are_rejected(state, query):
    *_, recall = state
    with pytest.raises(RecallError) as caught:
        recall.search(query)
    assert caught.value.code == "invalid_query"


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 6}, {"limit": True}])
def test_invalid_search_limits(state, kwargs):
    *_, recall = state
    with pytest.raises(RecallError):
        recall.search("hello", **kwargs)


@pytest.mark.parametrize("before,after", [(-1, 0), (4, 0), (0, 4), (True, 0)])
def test_invalid_windows_are_rejected(state, before, after):
    *_, recall = state
    with pytest.raises(RecallError):
        recall.get("some-id", before, after)


@pytest.mark.parametrize(
    "query", ["%", "_", "\\", 'say "hi"', "'; DROP TABLE messages; --", "OR NEAR NOT"]
)
def test_sql_and_fts_operators_are_literal_data(state, query):
    store, _, conversation, recall = state
    turn(store, conversation.id, user="literal " + query, answer="normal")
    assert len(recall.search(query).hits) == 1
    assert len(store.load_messages(conversation.id)) == 2
    assert not recall.search("not_a_real_phrase OR normal").hits


def test_bounded_short_query_scope_is_reported_and_does_not_scan_old_history(state):
    store, _, conversation, recall = state
    turn(store, conversation.id, user="旧词", answer="old")
    for index in range(RECENT_LIMIT // 2):
        turn(store, conversation.id, user=f"recent {index}", answer="ok")
    result = recall.search("旧词")
    assert result.scope_limited and result.hits == []
    assert recall.search("recent 0").hits  # Long queries still search the full index.


def test_short_queries_bound_scanned_text_prefix(state):
    store, _, conversation, recall = state
    turn(store, conversation.id, user="x" * SCAN_CHARS + "尾词", answer="ok")
    assert not recall.search("尾词").hits
    assert recall.search("x尾词").hits


def test_index_damage_is_diagnostic_and_explicit_rebuild_preserves_history(state):
    store, _, conversation, recall = state
    turn(store, conversation.id)
    original = store.load_messages(conversation.id)
    expected = recall.search("Memory")
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE recall_fts")
    with pytest.raises(RecallError) as caught:
        recall.search("Memory")
    assert caught.value.code == "recall_unavailable"
    with pytest.raises(HistoryError):
        store.begin_run(conversation.id, TextMessage(role="user", text="must roll back"))
    assert len(store.list_runs(conversation.id)) == 1
    store.rebuild_recall_index()
    assert store.load_messages(conversation.id) == original
    assert recall.search("Memory") == expected
    turn(store, conversation.id, user="after rebuild", answer="ok")
    assert recall.search("after rebuild").hits


def test_rebuild_recovers_missing_tokens_and_never_returns_forged_index_text(state):
    store, _, conversation, recall = state
    turn(store, conversation.id, user="canonical phrase", answer="ok")
    original = store.load_messages(conversation.id)
    with sqlite3.connect(store.path) as db:
        db.execute(
            "INSERT INTO recall_fts(recall_fts,rowid,text) "
            "SELECT 'delete',rowid,text FROM recall_text"
        )
        rowid = db.execute("SELECT rowid FROM messages WHERE id=?", (original[0].id,)).fetchone()[0]
        db.execute("INSERT INTO recall_fts(rowid,text) VALUES (?,?)", (rowid, "forged secret"))
    assert recall.search("canonical").hits == []
    assert recall.search("forged secret").hits == []
    assert recall.get(original[0].id).messages[0].text == "canonical phrase"
    store.rebuild_recall_index()
    assert recall.search("canonical").hits[0].message_id == original[0].id
    assert store.load_messages(conversation.id) == original


def test_index_and_messages_rollback_together_on_insert_failure(state):
    store, _, conversation, recall = state
    with sqlite3.connect(store.path) as db:
        db.execute(
            "CREATE TRIGGER reject_insert AFTER INSERT ON messages "
            "BEGIN SELECT RAISE(ABORT,'private failure'); END"
        )
    with pytest.raises(HistoryError):
        turn(store, conversation.id, user="rolledback marker")
    assert store.load_messages(conversation.id) == []
    assert store.list_runs(conversation.id) == []
    assert recall.search("rolledback marker").hits == []


def test_update_delete_triggers_track_authoritative_text(state):
    store, _, conversation, recall = state
    turn(store, conversation.id, user="originalkeyword", answer="ok")
    message_id = store.load_messages(conversation.id)[0].id
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE messages SET payload=? WHERE id=?",
            (TextMessage(role="user", text="replacementkeyword").model_dump_json(), message_id),
        )
    assert recall.search("originalkeyword").hits == []
    assert recall.search("replacementkeyword").hits[0].message_id == message_id
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM messages WHERE id=?", (message_id,))
    assert recall.search("replacementkeyword").hits == []


def test_failed_rebuild_rolls_back_derived_objects_and_preserves_messages(state, monkeypatch):
    from personal_agent.storage import recall as index

    store, _, conversation, recall = state
    turn(store, conversation.id)
    before = recall.search("Memory")
    canonical = store.load_messages(conversation.id)
    monkeypatch.setattr(index, "INDEX_SCHEMA", (*index.INDEX_SCHEMA, "INVALID SQL"))
    with pytest.raises(HistoryError):
        store.rebuild_recall_index()
    assert recall.search("Memory") == before
    assert store.load_messages(conversation.id) == canonical


def test_malformed_json_update_is_rejected_with_index_and_history_unchanged(state):
    store, _, conversation, recall = state
    turn(store, conversation.id)
    canonical = store.load_messages(conversation.id)
    with sqlite3.connect(store.path) as db, pytest.raises(sqlite3.OperationalError):
        db.execute("UPDATE messages SET payload='invalid private JSON'")
    assert store.load_messages(conversation.id) == canonical
    assert len(recall.search("Memory").hits) == 2
