import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from personal_agent.core.budget import ContextBudget
from personal_agent.core.history import HistoryError
from personal_agent.core.models import (
    AssistantMessage,
    ModelResponse,
    RunResult,
    TextMessage,
    TokenUsage,
    ToolCall,
)
from personal_agent.organization.episodes import EpisodeOrganizer
from personal_agent.organization.models import OrganizationPolicy
from personal_agent.organization.provider import OrganizerModel
from personal_agent.sessions import SessionManager
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.sqlite import SQLiteStore
from tests.fakes import FakeProvider


def seed(store, stream, text="我们决定先实现 HeyBuddy P1", *, at=None, status="completed"):
    run_id = store.begin_run(stream, TextMessage(role="user", text=text))
    if status in {"completed", "failed"}:
        store.append_message(run_id, AssistantMessage(text="这是建议，尚待确认。"))
        store.finish_run(
            run_id,
            RunResult(status="completed" if status == "completed" else "provider_error", rounds=1),
        )
    elif status != "running":
        store.interrupt_run(run_id, cancelled=status == "cancelled")
    if at is not None:
        with store._transaction() as db:
            db.execute(
                "UPDATE runs SET started_at=?,ended_at=? WHERE id=?",
                (at.isoformat(), (at + timedelta(seconds=1)).isoformat(), run_id),
            )
    return run_id


class SegmentProvider(FakeProvider):
    def __init__(self, *, split=False, mutate=None):
        super().__init__()
        self.split = split
        self.mutate = mutate

    async def generate(self, messages, tools, *, max_output_tokens=None):
        data = json.loads(messages[1].text)
        ids = [r["run_id"] for r in data["runs"]]
        required = {b["before_run_id"] for b in data["boundaries"] if b["strength"] == "required"}
        groups = []
        for rid in ids:
            if not groups or self.split or rid in required:
                groups.append([])
            groups[-1].append(rid)
        output = {
            "episodes": [
                {
                    "run_ids": group,
                    "title": "HeyBuddy",
                    "summary": "讨论当前实现。",
                    "highlights": [],
                }
                for group in groups
            ]
        }
        if self.mutate:
            self.mutate(output, data)
        self.responses.append(ModelResponse(text=json.dumps(output, ensure_ascii=False)))
        return await super().generate(messages, tools, max_output_tokens=max_output_tokens)


@pytest.fixture
def state(tmp_path):
    with SQLiteStore(tmp_path / "episodes.db") as store:
        store.initialize()
        sessions = SessionManager(store)
        yield store, sessions.current.id, EpisodeRepository(store, sessions.agent.id)


def organizer(repo, provider=None, **changes):
    return EpisodeOrganizer(
        repo,
        OrganizerModel(
            provider or SegmentProvider(),
            ContextBudget(context_window=131072),
            OrganizationPolicy(**changes),
        ),
    )


@pytest.mark.parametrize("gap,split,expected", [(60, False, 1), (60, True, 2), (14400, False, 2)])
async def test_temporal_and_semantic_boundaries(state, gap, split, expected):
    db, stream, repo = state
    stamp = datetime.now(UTC) - timedelta(days=1)
    seed(db, stream, at=stamp)
    seed(db, stream, at=stamp + timedelta(seconds=gap))
    provider = SegmentProvider(split=split)
    report = await organizer(repo, provider).organize(stream)
    assert report["outcome"] == "completed"
    assert len(repo.list(stream)) == expected
    assert provider.tools == [[]]
    assert provider.output_limits == [8192]


async def test_midnight_is_not_a_boundary(state):
    db, stream, repo = state
    stamp = datetime(2025, 1, 1, 23, 59, tzinfo=UTC)
    ids = [seed(db, stream, at=stamp), seed(db, stream, at=stamp + timedelta(minutes=2))]
    await organizer(repo).organize(stream)
    assert repo.list(stream)[0].run_ids == ids


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
async def test_terminal_exclusions_are_barriers_and_do_not_block_progress(state, status):
    db, stream, repo = state
    first = seed(db, stream)
    excluded = seed(db, stream, status=status)
    last = seed(db, stream)
    service = organizer(repo)
    report = await service.organize(stream)
    assert report["skipped"] == 1 and len(repo.list(stream)) == 2
    assert {rid for e in repo.list(stream) for rid in e.run_ids} == {first, last}
    with db._transaction() as connection:
        assert (
            connection.execute(
                "SELECT reason FROM episode_skips WHERE run_id=?", (excluded,)
            ).fetchone()[0]
            == status
        )
    assert (await service.organize(stream))["outcome"] == "no_history"


@pytest.mark.parametrize(
    "bad",
    [
        "duplicate",
        "unknown",
        "omitted",
        "reorder",
        "time",
        "extra",
        "decision",
        "quote",
        "cross_segment",
    ],
)
async def test_invalid_model_partition_and_evidence_never_advance(state, bad):
    db, stream, repo = state
    seed(db, stream)
    seed(db, stream)

    def mutate(output, data):
        segment = output["episodes"][0]
        if bad == "duplicate":
            segment["run_ids"].append(segment["run_ids"][0])
        elif bad == "unknown":
            segment["run_ids"][0] = "missing"
        elif bad == "omitted":
            segment["run_ids"].pop()
        elif bad == "reorder":
            segment["run_ids"].reverse()
        elif bad in {"time", "extra"}:
            segment["started_at" if bad == "time" else "write_target"] = "bad"
        else:
            source = data["runs"][0]
            message = source["messages"][1 if bad == "decision" else 0]
            segment["highlights"] = [
                {
                    "kind": "decision",
                    "content": "已经批准了",
                    "attribution": "user_confirmed",
                    "sources": [
                        {
                            "conversation_id": stream,
                            "run_id": "other" if bad == "cross_segment" else source["run_id"],
                            "sequence": message["sequence"],
                            "quote": "does not exist" if bad == "quote" else message["text"],
                        }
                    ],
                }
            ]

    report = await organizer(repo, SegmentProvider(mutate=mutate)).organize(stream)
    assert report["outcome"] == "failed"
    assert repo.status(stream)["position"] == 0 and repo.list(stream) == []


async def test_verified_highlight_retains_user_and_source_time(state):
    db, stream, repo = state
    run_id = seed(db, stream)

    def mutate(output, data):
        output["episodes"][0]["highlights"] = [
            {
                "kind": "decision",
                "content": "实现 P1",
                "attribution": "user_confirmed",
                "sources": [
                    {
                        "conversation_id": stream,
                        "run_id": run_id,
                        "sequence": data["runs"][0]["first_sequence"],
                        "quote": "决定先实现 HeyBuddy",
                    }
                ],
            }
        ]

    assert (await organizer(repo, SegmentProvider(mutate=mutate)).organize(stream))["outcome"] == (
        "completed"
    )
    episode = repo.list(stream)[0]
    assert episode.highlights[0].sources[0].run_id == run_id
    assert episode.started_at.tzinfo is not None


async def test_provisional_append_split_revision_finalize_and_idempotence(state):
    db, stream, repo = state
    first = seed(db, stream)
    provider = SegmentProvider()
    service = organizer(repo, provider)
    await service.organize(stream)
    original = repo.list(stream)[0]
    assert original.status == "provisional"
    second = seed(db, stream)
    await service.organize(stream)
    extended = repo.get(original.id)
    assert extended.run_ids == [first, second] and extended.revision == 2
    seed(db, stream)
    provider.split = True
    await service.organize(stream)
    assert len(repo.list(stream)) == 3
    assert repo.get(original.id).run_ids == [first]
    assert repo.get(original.id).status == "finalized"
    assert (await service.organize(stream))["outcome"] == "no_history"
    assert len(provider.requests) == 3
    with db._transaction() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM episode_versions WHERE episode_id=?", (original.id,)
            ).fetchone()[0]
            == 3
        )


async def test_running_stops_cursor_and_oversized_is_explicitly_skipped(state):
    db, stream, repo = state
    seed(db, stream, text="x" * 4000)
    active = seed(db, stream, status="running")
    service = organizer(repo, source_bytes=1024)
    assert (await service.organize(stream))["skipped"] == 1
    assert not repo.list(stream)
    position = repo.status(stream)["position"]
    assert (await service.organize(stream))["outcome"] == "no_history"
    assert repo.status(stream)["position"] == position
    db.interrupt_run(active)
    assert (await service.organize(stream))["skipped"] == 1


@pytest.mark.parametrize(
    "response",
    [
        ModelResponse(text="bad json"),
        ModelResponse(text="x" * 33000),
        ModelResponse(text='{"episodes":[]}'),
    ],
)
async def test_invalid_output_is_safe_and_retry_is_explicit(state, response):
    db, stream, repo = state
    seed(db, stream)
    provider = FakeProvider(response)
    service = organizer(repo, provider)
    assert (await service.organize(stream))["outcome"] == "failed"
    assert (await service.organize(stream, retry=False))["outcome"] == "blocked"
    assert len(provider.requests) == 1
    assert repo.status(stream)["position"] == 0
    assert (await organizer(repo).organize(stream))["outcome"] == "completed"


async def test_source_mutation_and_database_failure_rollback(state):
    db, stream, repo = state
    seed(db, stream)

    def mutate(output, data):
        with db._transaction() as connection:
            connection.execute(
                "UPDATE runs SET ended_at=? WHERE id=?",
                ((datetime.now(UTC) + timedelta(seconds=2)).isoformat(), data["runs"][0]["run_id"]),
            )

    assert (await organizer(repo, SegmentProvider(mutate=mutate)).organize(stream))["outcome"] == (
        "failed"
    )
    with db._transaction() as connection:
        connection.execute(
            "CREATE TRIGGER reject_episode BEFORE INSERT ON episodes "
            "BEGIN SELECT RAISE(ABORT,'private'); END"
        )
    assert (await organizer(repo).organize(stream))["outcome"] == "failed"
    assert repo.status(stream)["position"] == 0 and repo.list(stream) == []


async def test_cancellation_no_long_transaction_restart_and_bounded_pages(state, monkeypatch):
    db, stream, repo = state
    for _ in range(10):
        seed(db, stream)
    monkeypatch.setattr(db, "load_messages", lambda *a, **k: pytest.fail("full history"))
    entered = asyncio.Event()

    class Slow:
        async def generate(self, *args, **kwargs):
            assert not db._connection.in_transaction
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(organizer(repo, Slow()).organize(stream))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert repo.status(stream)["position"] == 0
    repo.recover()
    service = organizer(repo)
    await service.organize(stream)
    assert sum(len(e.run_ids) for e in repo.list(stream)) == 8
    await service.organize(stream)
    assert sum(len(e.run_ids) for e in repo.list(stream)) == 10


def test_schema_five_upgrade_preserves_old_streams_summary_and_recall(tmp_path, monkeypatch):
    from personal_agent.storage import sqlite as storage

    path = tmp_path / "old.db"
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 5)
        with SQLiteStore(path) as db:
            db.initialize()
            sessions = SessionManager(db)
            stream = sessions.current.id
            seed(db, stream)
            sessions.new()
            selected = sessions.current.id
            original = db.load_messages(stream)
    with SQLiteStore(path) as db:
        db.initialize()
        assert db.load_messages(stream) == original
        assert SessionManager(db).current.id == selected
        assert len(db.search_recall("heybuddy", "HeyBuddy", 5)) > 0
        assert db._connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db._connection.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION


def test_schema_six_migration_is_atomic(tmp_path, monkeypatch):
    from personal_agent.storage import sqlite as storage

    path = tmp_path / "atomic.db"
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 5)
        with SQLiteStore(path) as db:
            db.initialize()
    with SQLiteStore(path) as db:
        monkeypatch.setitem(storage.MIGRATIONS, 6, (*storage.MIGRATIONS[6], "INVALID SQL"))
        with pytest.raises(Exception, match="database operation"):
            db.initialize()
        assert db._connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert not db._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='episodes'"
        ).fetchone()


@pytest.mark.parametrize(
    "response",
    [
        ModelResponse(text="{}", usage=TokenUsage(input_tokens=1, output_tokens=9000)),
        ModelResponse(tool_calls=[ToolCall(id="bad", name="memory_update", arguments="{}")]),
        TimeoutError("private error body"),
    ],
)
async def test_usage_tools_and_provider_errors_are_nonwriting(state, response):
    db, stream, repo = state
    seed(db, stream)
    report = await organizer(repo, FakeProvider(response)).organize(stream)
    assert report["outcome"] == "failed" and repo.status(stream)["position"] == 0
    assert "private" not in json.dumps(report)


async def test_revision_and_source_hash_compare_and_swap(state, monkeypatch):
    db, stream, repo = state
    run = seed(db, stream)
    service = organizer(repo)
    captured = []
    publish = repo.publish

    def capture(*args):
        captured.append(args)
        return publish(*args)

    monkeypatch.setattr(repo, "publish", capture)
    await service.organize(stream)
    with pytest.raises(HistoryError):
        publish(*captured[0])
    seed(db, stream)

    def change_old(output, data):
        with db._transaction() as connection:
            connection.execute(
                "UPDATE messages SET payload=replace(payload,?,?) WHERE run_id=?",
                ("HeyBuddy", "HeyBuddz", run),
            )

    report = await organizer(repo, SegmentProvider(mutate=change_old)).organize(stream)
    assert report["outcome"] == "failed"
    assert repo.list(stream)[0].revision == 1


async def test_stable_finalize_without_model_and_reversed_time_rejected(state):
    db, stream, repo = state
    seed(db, stream, at=datetime.now(UTC) - timedelta(seconds=2))
    await organizer(repo).organize(stream)
    assert repo.finalize(stream, OrganizationPolicy(revision_seconds=1))
    assert repo.list(stream)[0].status == "finalized"
    seed(db, stream, at=datetime.now(UTC))
    seed(db, stream, at=datetime.now(UTC) - timedelta(hours=1))
    report = await organizer(repo).organize(stream)
    assert report["error_code"] == "nonmonotonic_time"
