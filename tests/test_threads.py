import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from personal_agent.core.budget import ContextBudget
from personal_agent.core.history import HistoryError
from personal_agent.core.models import ModelResponse
from personal_agent.organization.models import OrganizationPolicy
from personal_agent.organization.provider import OrganizerModel
from personal_agent.organization.threads import ThreadOrganizer
from personal_agent.storage.threads import ThreadRepository
from tests.fakes import FakeProvider
from tests.test_episodes import SegmentProvider, organizer, seed, state

__all__ = ["state"]


class TopicProvider(FakeProvider):
    def __init__(self, *, no_thread=False, mutate=None):
        super().__init__()
        self.no_thread = no_thread
        self.mutate = mutate

    async def generate(self, messages, tools, *, max_output_tokens=None):
        data = json.loads(messages[1].text)
        if "previous_rebuild" in data:
            response = {
                "current_state": data["previous_rebuild"]
                + " ".join(e["summary"] for e in data["episodes"]),
                "status": "active",
            }
            self.responses.append(ModelResponse(text=json.dumps(response, ensure_ascii=False)))
            return await super().generate(messages, tools, max_output_tokens=max_output_tokens)
        episodes = data["episodes"]
        existing = data["existing_threads"]
        ids = [e["id"] for e in episodes]
        output = {"groups": [], "no_thread": []}
        if self.no_thread:
            output["no_thread"] = ids
        else:
            output["groups"] = [
                {
                    "episode_ids": ids,
                    "title": "HeyBuddy",
                    "thread_id": existing[0]["id"] if existing else None,
                    "current_state": episodes[-1]["summary"],
                    "status": "active",
                }
            ]
        if self.mutate:
            self.mutate(output, data)
        self.responses.append(ModelResponse(text=json.dumps(output, ensure_ascii=False)))
        return await super().generate(messages, tools, max_output_tokens=max_output_tokens)


def thread_service(db, provider=None, **policy):
    return ThreadOrganizer(
        ThreadRepository(db, "heybuddy"),
        OrganizerModel(
            provider or TopicProvider(),
            ContextBudget(context_window=131072),
            OrganizationPolicy(**policy),
        ),
    )


async def seed_days(db, stream, repo, days=2):
    stamp = datetime.now(UTC) - timedelta(days=days + 1)
    for i in range(days):
        seed(db, stream, at=stamp + timedelta(days=i))
    await organizer(repo).organize(stream)


async def test_cross_day_episodes_share_thread_with_idempotent_latest_state(state):
    db, stream, repo = state
    await seed_days(db, stream, repo)
    provider = TopicProvider()
    service = thread_service(db, provider)
    report = await service.organize()
    assert report["outcome"] == "completed"
    threads = service.repository.list()
    assert len(threads) == 1
    first = threads[0]
    assert all(e.thread_id == first.id for e in repo.list(stream))
    assert (await service.organize())["outcome"] == "no_episodes"
    assert len(provider.requests) == 1 and provider.tools == [[]]
    seed(db, stream)
    await organizer(repo).organize(stream)
    assert (await service.organize())["outcome"] == "completed"
    updated = service.repository.get(first.id)
    assert updated.revision == 2 and updated.last_active_at > first.last_active_at
    assert len(service.repository.list()) == 1
    assert not any(e.thread_dirty for e in repo.list(stream))
    with db._transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM thread_versions").fetchone()[0] == 2
        version = connection.execute(
            "SELECT payload FROM thread_versions WHERE thread_id=? AND revision=?",
            (updated.id, updated.revision),
        ).fetchone()[0]
        assert json.loads(version)["source_revision"] == updated.source_revision
        assert json.loads(version)["dirty"] == updated.dirty
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


async def test_small_talk_can_have_no_thread_and_is_not_reprocessed(state):
    db, stream, repo = state
    seed(db, stream, text="今天吃了好吃的饭")
    await organizer(repo).organize(stream)
    service = thread_service(db, TopicProvider(no_thread=True))
    assert (await service.organize())["outcome"] == "completed"
    assert service.repository.list() == []
    episode = repo.list(stream)[0]
    assert episode.thread_id is None and not episode.thread_dirty
    assert (await service.organize())["outcome"] == "no_episodes"


@pytest.mark.parametrize(
    "bad",
    [
        "duplicate",
        "missing",
        "foreign_episode",
        "unknown_thread",
        "extra_field",
        "duplicate_thread",
    ],
)
async def test_invalid_assignments_do_not_publish_or_clear_progress(state, bad):
    db, stream, repo = state
    await seed_days(db, stream, repo)
    if bad == "duplicate_thread":
        await thread_service(db).organize()
        seed(db, stream)
        await organizer(repo, SegmentProvider(split=True)).organize(stream)
        seed(db, stream)
        await organizer(repo, SegmentProvider(split=True)).organize(stream)

    def mutate(output, data):
        group = output["groups"][0]
        if bad == "duplicate":
            output["no_thread"].append(group["episode_ids"][0])
        elif bad == "missing":
            group["episode_ids"].pop()
        elif bad == "foreign_episode":
            group["episode_ids"][0] = "foreign"
        elif bad == "unknown_thread":
            group["thread_id"] = "unprovided"
        elif bad == "extra_field":
            group["memory_ref"] = "knowledge/forbidden.md"
        else:
            second = dict(group, episode_ids=[group["episode_ids"].pop()])
            output["groups"].append(second)

    service = thread_service(db, TopicProvider(mutate=mutate))
    before = repo.list(stream)
    assert (await service.organize())["outcome"] == "failed"
    assert repo.list(stream) == before
    assert service.repository.status()["state"] == "failed"
    assert (await service.organize(retry=False))["outcome"] == "blocked"


async def test_episode_revision_dirties_thread_and_relinks_split_consistently(state):
    db, stream, repo = state
    seed(db, stream)
    episode_service = organizer(repo, SegmentProvider(split=True))
    await episode_service.organize(stream)
    service = thread_service(db)
    await service.organize()
    original = repo.list(stream)[0]
    seed(db, stream)
    await episode_service.organize(stream)
    assert len(repo.list(stream)) == 2
    assert repo.get(original.id).thread_id == original.thread_id
    assert service.repository.get(original.thread_id).dirty
    assert (await service.organize())["outcome"] == "completed"
    assert all(e.thread_id == original.thread_id for e in repo.list(stream))
    assert service.repository.get(original.thread_id).dirty
    assert (await service.repair())["outcome"] == "repaired"
    assert not service.repository.get(original.thread_id).dirty
    assert len(service.repository.list()) == 1


async def test_stale_thread_and_episode_cas_conflicts_preserve_links(state):
    db, stream, repo = state
    await seed_days(db, stream, repo)

    def mutate(output, data):
        with db._transaction() as connection:
            connection.execute(
                "UPDATE episodes SET revision=revision+1 WHERE id=?", (data["episodes"][0]["id"],)
            )

    service = thread_service(db, TopicProvider(mutate=mutate))
    assert (await service.organize())["outcome"] == "failed"
    assert service.repository.list() == []


async def test_database_failure_rolls_back_threads_assignments_and_versions(state):
    db, stream, repo = state
    await seed_days(db, stream, repo)
    before = repo.list(stream)
    with db._transaction() as connection:
        connection.execute(
            "CREATE TRIGGER reject_thread_batch BEFORE INSERT ON thread_batches "
            "BEGIN SELECT RAISE(ABORT,'private'); END"
        )
    service = thread_service(db)
    report = await service.organize()
    assert report["outcome"] == "failed" and "private" not in json.dumps(report)
    assert service.repository.list() == [] and repo.list(stream) == before


async def test_thread_cancellation_and_restart_recover_pending_sources(state):
    db, stream, repo = state
    await seed_days(db, stream, repo)
    entered = asyncio.Event()

    class Slow:
        async def generate(self, *args, **kwargs):
            assert not db._connection.in_transaction
            entered.set()
            await asyncio.Event().wait()

    service = thread_service(db, Slow())
    task = asyncio.create_task(service.organize())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.repository.status()["revision"] == 0
    service.repository.recover()
    assert (await thread_service(db).organize())["outcome"] == "completed"


async def test_thread_ownership_is_enforced_in_repository_and_sql(state):
    db, stream, repo = state
    seed(db, stream)
    await organizer(repo).organize(stream)
    await thread_service(db).organize()
    thread_id = repo.list(stream)[0].thread_id
    other = db.create_agent("Other", "base")
    with pytest.raises(HistoryError):
        ThreadRepository(db, other.id).get(thread_id)
    foreign = db.create_conversation(other.id)
    episode_id = repo.list(stream)[0].id
    with pytest.raises(HistoryError):
        with db._transaction() as connection:
            connection.execute(
                "UPDATE episodes SET stream_id=? WHERE id=?", (foreign.id, episode_id)
            )


async def test_low_frequency_runtime_hook_and_thread_commands(state):
    from personal_agent.organization.runtime import OrganizationRuntime

    db, stream, repo = state
    await seed_days(db, stream, repo, days=3)
    service = thread_service(db)
    runtime = OrganizationRuntime(organizer(repo, idle_seconds=0.01), threads=service)
    try:
        runtime.schedule(stream)
        for _ in range(100):
            if service.repository.list():
                break
            await asyncio.sleep(0.01)
        assert len(service.repository.list()) == 1
        await runtime.foreground()
        output = []
        for command in ["/thread status", "/thread list", "/thread organize"]:
            await runtime.command(stream, command, output.append)
        assert json.loads(output[-1])["outcome"] == "no_episodes"
    finally:
        await runtime.close()


async def test_status_only_finalization_keeps_semantically_current_thread_state(state):
    db, stream, repo = state
    seed(db, stream, at=datetime.now(UTC) - timedelta(seconds=2))
    await organizer(repo).organize(stream)
    service = thread_service(db)
    await service.organize()
    episode = repo.list(stream)[0]
    assert repo.finalize(stream, OrganizationPolicy(revision_seconds=1))
    assert repo.get(episode.id).revision == episode.revision + 1
    assert not repo.get(episode.id).thread_dirty
    assert not service.repository.get(episode.thread_id).dirty


def test_schema_seven_migration_failure_preserves_episode_tables(tmp_path, monkeypatch):
    from personal_agent.storage import sqlite as storage
    from personal_agent.storage.sqlite import SQLiteStore

    path = tmp_path / "six.db"
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 6)
        with SQLiteStore(path) as db:
            db.initialize()
    with SQLiteStore(path) as db:
        monkeypatch.setitem(storage.MIGRATIONS, 7, (*storage.MIGRATIONS[7], "INVALID SQL"))
        with pytest.raises(HistoryError):
            db.initialize()
        assert db._connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert not db._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='thread_versions'"
        ).fetchone()
