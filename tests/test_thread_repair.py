import asyncio
import json

import pytest

from personal_agent.core.history import HistoryError
from personal_agent.core.models import ModelResponse
from personal_agent.organization.models import ThreadDecision
from tests.fakes import FakeProvider
from tests.test_episodes import state
from tests.test_threads import TopicProvider, seed_days, thread_service

__all__ = ["state"]


async def linked(state):
    db, stream, episodes = state
    await seed_days(db, stream, episodes, days=3)
    ordered = sorted(episodes.list(stream), key=lambda e: e.started_at)
    decision = {
        "groups": [
            {
                "episode_ids": [ordered[0].id, ordered[1].id],
                "title": "X",
                "current_state": "old A and B",
            },
            {"episode_ids": [ordered[2].id], "title": "Y", "current_state": "C"},
        ],
        "no_thread": [],
    }
    service = thread_service(db, FakeProvider(ModelResponse(text=json.dumps(decision))))
    assert (await service.organize())["outcome"] == "completed"
    a, b, c = [episodes.get(e.id) for e in ordered]
    return thread_service(db), a, b, c


def move(service, episode, target):
    repo = service.repository
    with repo.store._transaction() as connection:
        connection.execute("UPDATE episodes SET thread_dirty=1 WHERE id=?", (episode.id,))
    revision, episodes, candidates = repo.snapshot(1)
    repo.attempt(revision, "running")
    decision = ThreadDecision.model_validate(
        {
            "groups": [
                {
                    "episode_ids": [episode.id],
                    "thread_id": target,
                    "title": "Y",
                    "current_state": "new target state",
                }
            ]
            if target
            else [],
            "no_thread": [] if target else [episode.id],
        }
    )
    repo.publish(revision, episodes, candidates, decision, {})


@pytest.mark.parametrize("no_thread", [False, True])
async def test_moved_or_detached_episode_is_never_old_thread_repair_evidence(state, no_thread):
    db, _, repo = state
    service, a, b, c = await linked(state)
    move(service, a, None if no_thread else c.thread_id)
    assert repo.get(a.id).thread_id == (None if no_thread else c.thread_id)
    assert service.repository.get(a.thread_id).dirty
    assert service.repository.status()["pending_episodes_capped"] == 0
    assert (await service.repair())["outcome"] == "repaired"
    request = json.loads(service.model.provider.requests[-1][1].text)
    assert {e["id"] for e in request["episodes"]} == {b.id}
    assert request["previous_rebuild"] == "" and "old A and B" not in json.dumps(request)
    assert not service.repository.get(a.thread_id).dirty
    assert (await service.repair())["outcome"] == "no_dirty_work"
    with db._transaction() as connection:
        assert [
            row[0]
            for row in connection.execute(
                "SELECT episode_id FROM thread_state_sources WHERE thread_id=?",
                (a.thread_id,),
            )
        ] == [b.id]
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()


async def test_empty_old_thread_is_archived_without_a_model_call(state):
    db, _, repo = state
    service, a, b, c = await linked(state)
    move(service, c, a.thread_id)
    assert (await service.repair())["empty_archived"]
    old = service.repository.get(c.thread_id)
    assert old.status == "closed" and not old.dirty and old.current_state != "C"
    assert not service.model.provider.requests
    with db._transaction() as connection:
        assert (
            connection.execute(
                "SELECT error_code FROM thread_repairs WHERE thread_id=?", (c.thread_id,)
            ).fetchone()[0]
            == "empty_archived"
        )


async def test_dirty_repair_pages_all_current_members_and_retains_checkpoint_after_cancel(state):
    db, _, repo = state
    service, a, b, c = await linked(state)
    with db._transaction() as connection:
        connection.execute("UPDATE topic_threads SET dirty=1 WHERE id=?", (a.thread_id,))
    small = thread_service(db, page_runs=1)
    first = await small.repair()
    assert first["outcome"] == "repair_page" and small.repository.get(a.thread_id).dirty
    entered = asyncio.Event()

    class Slow:
        async def generate(self, *args, **kwargs):
            assert not db._connection.in_transaction
            entered.set()
            await asyncio.Event().wait()

    small.model.provider = Slow()
    task = asyncio.create_task(small.repair())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with db._transaction() as connection:
        checkpoint = connection.execute(
            "SELECT source_count,state FROM thread_repairs WHERE thread_id=?", (a.thread_id,)
        ).fetchone()
        assert tuple(checkpoint) == (1, "pending")
    reopened = thread_service(db, page_runs=1)
    reopened.repository.recover()
    assert (await reopened.repair())["outcome"] == "repaired"
    assert not reopened.repository.get(a.thread_id).dirty
    with db._transaction() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM thread_state_sources WHERE thread_id=?", (a.thread_id,)
            ).fetchone()[0]
            == 2
        )


async def test_failed_repair_remains_pending_and_explicit_retry_can_complete(state):
    _, _, _ = state
    service, a, b, c = await linked(state)
    move(service, a, None)
    service.model.provider = FakeProvider(TimeoutError("private provider body"))
    report = await service.repair()
    assert report["outcome"] == "failed" and "private" not in json.dumps(report)
    assert service.repository.get(a.thread_id).dirty
    assert (await service.repair(retry=False))["outcome"] == "no_dirty_work"
    service.model.provider = TopicProvider()
    assert (await service.repair())["outcome"] == "repaired"


async def test_membership_changes_during_model_call_reject_old_rebuild(state):
    db, _, _ = state
    service, a, b, c = await linked(state)
    move(service, a, None)

    class Concurrent(TopicProvider):
        async def generate(self, *args, **kwargs):
            result = await super().generate(*args, **kwargs)
            with db._transaction() as connection:
                connection.execute(
                    "UPDATE episodes SET thread_id=? WHERE id=?", (c.thread_id, b.id)
                )
            return result

    service.model.provider = Concurrent()
    assert (await service.repair())["outcome"] == "failed"
    assert service.repository.get(a.thread_id).dirty
    service.model.provider = TopicProvider()
    report = await service.repair(retry=False)
    assert report["outcome"] == "repaired" and report["empty_archived"]


def test_schema_eight_failure_is_atomic_and_does_not_rewrite_old_migrations(tmp_path, monkeypatch):
    from personal_agent.storage import sqlite as storage

    path = tmp_path / "old.db"
    old = {n: storage.MIGRATIONS[n] for n in range(1, 8)}
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 7)
        with storage.SQLiteStore(path) as db:
            db.initialize()
    with storage.SQLiteStore(path) as db:
        monkeypatch.setitem(storage.MIGRATIONS, 8, (*storage.MIGRATIONS[8], "INVALID SQL"))
        with pytest.raises(HistoryError):
            db.initialize()
        assert db._connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert "source_revision" not in {
            r[1] for r in db._connection.execute("PRAGMA table_info(topic_threads)")
        }
    assert {n: storage.MIGRATIONS[n] for n in range(1, 8)} == old
