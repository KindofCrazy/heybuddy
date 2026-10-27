import sqlite3

import pytest

from personal_agent.character.storage import CharacterStateRepository
from personal_agent.core.history import HistoryError
from personal_agent.core.models import TextMessage
from personal_agent.sessions import SessionManager
from personal_agent.storage import sqlite as storage
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.sqlite import SQLiteStore
from tests.test_character_state import episode, state
from tests.test_episodes import seed

__all__ = ["state"]


def test_schema_11_to_12_is_atomic_and_preserves_old_journal(tmp_path, monkeypatch):
    path = tmp_path / "migration.db"
    with SQLiteStore(path) as db:
        monkeypatch.setattr(storage, "SCHEMA_VERSION", 11)
        db.initialize()
        sessions = SessionManager(db)
        seed(db, sessions.current.id, "旧的普通 HeyBuddy 对话不能被重新标为 Sherlock。")
        before = db.load_messages(sessions.current.id)
        monkeypatch.setattr(storage, "SCHEMA_VERSION", 12)
        original = storage.MIGRATIONS[12]
        monkeypatch.setitem(storage.MIGRATIONS, 12, (*original, "INVALID SQL"))
        with pytest.raises(HistoryError):
            db.initialize()
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
            assert not connection.execute(
                "SELECT name FROM sqlite_master WHERE name='character_runs'"
            ).fetchall()
        monkeypatch.setitem(storage.MIGRATIONS, 12, original)
        db.initialize()
        assert db.load_messages(sessions.current.id) == before
        with db._transaction() as connection:
            assert connection.execute("SELECT COUNT(*) FROM character_runs").fetchone()[0] == 0


def test_agent_scope_is_enforced_by_host_and_insert_trigger(state):
    db, sessions, memory, clock, provider, actor = state
    other = db.create_agent("other", "prompt", agent_id="other")
    foreign = CharacterStateRepository(
        EpisodeRepository(db, other.id), actor.package.character_id, actor.package.fingerprint
    )
    foreign.initialize()
    rid = db.begin_run(sessions.current.id, TextMessage(role="user", text="仍属于原 Agent"))
    with pytest.raises(HistoryError):
        foreign.tag_run(rid)
    with pytest.raises(HistoryError):
        with db._transaction() as connection:
            connection.execute("INSERT INTO character_runs VALUES (?,?,?,?)", (rid, *foreign.scope))
    assert foreign.current() is None
    actor.repository.tag_run(rid)
    db.interrupt_run(rid)
    assert foreign.candidate() is None


async def test_state_and_consumed_source_commit_or_rollback_together(state):
    db, sessions, memory, clock, provider, actor = state
    await episode(state)
    with db._transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_character_receipt BEFORE UPDATE ON "
            "character_state_sources WHEN NEW.state='completed' "
            "BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
        )
    result = await actor.run(force=True)
    assert result["outcome"] == "failed"
    assert actor.repository.current() is None
    assert actor.repository.row()["revision"] == 0
    assert actor.status()["counts"] == {"failed": 1}
    with db._transaction() as connection:
        connection.execute("DROP TRIGGER fail_character_receipt")
    assert (await actor.run(force=True, retry=True))["updated"]
    assert actor.repository.current().revision == 1
