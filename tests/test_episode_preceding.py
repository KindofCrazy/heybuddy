"""Production Episode look-behind stays bounded and outside published evidence."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from personal_agent.core.history import HistoryError
from personal_agent.core.models import AssistantMessage, RunResult, TextMessage
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.routing import RoutingRepository
from tests.test_episodes import SegmentProvider, organizer, seed, state

__all__ = ["state"]


async def test_preceding_run_is_background_only_without_republishing_or_journal_changes(state):
    store, stream, repo = state
    first = seed(store, stream, text="周六交插画。")
    provider = SegmentProvider()
    service = organizer(repo, provider, page_runs=1)
    assert (await service.organize(stream))["outcome"] == "completed"
    original = repo.list(stream)[0]
    second = seed(store, stream, text="交给编辑。")
    journal = store.load_messages(stream)
    assert (await service.organize(stream))["outcome"] == "completed"
    data = json.loads(provider.requests[-1][1].text)
    assert data["preceding_context"]["run_id"] == first
    assert data["preceding_context"]["messages"][0]["text"] == "周六交插画。"
    assert [run["run_id"] for run in data["runs"]] == [second]
    assert repo.get(original.id) == original
    assert sorted(e.run_ids for e in repo.list(stream)) == sorted([[first], [second]])
    assert store.load_messages(stream) == journal
    assert provider.tools == [[], []]


@pytest.mark.parametrize("misuse", ["partition", "citation"])
async def test_preceding_run_cannot_be_assigned_or_cited_and_failure_preserves_progress(
    state, misuse
):
    store, stream, repo = state
    seed(store, stream)
    await organizer(repo, page_runs=1).organize(stream)
    seed(store, stream, text="交给编辑。")
    before = repo.status(stream)["position"]
    episodes = repo.list(stream)

    def mutate(output, data):
        preceding = data["preceding_context"]
        segment = output["episodes"][0]
        if misuse == "partition":
            segment["run_ids"].insert(0, preceding["run_id"])
        else:
            message = preceding["messages"][0]
            segment["highlights"] = [
                dict(
                    kind="open_loop",
                    content="此前的事项",
                    attribution="user_confirmed",
                    sources=[
                        dict(
                            conversation_id=stream,
                            run_id=preceding["run_id"],
                            sequence=message["sequence"],
                            quote=message["text"],
                        )
                    ],
                )
            ]

    report = await organizer(repo, SegmentProvider(mutate=mutate), page_runs=1).organize(stream)
    assert report["error_code"] == (
        "invalid_partition" if misuse == "partition" else "invalid_evidence"
    )
    assert repo.status(stream)["position"] == before
    assert repo.list(stream) == episodes


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted", "running"])
def test_preceding_lookup_never_skips_an_exclusion_barrier(state, status):
    store, stream, repo = state
    seed(store, stream)
    seed(store, stream, status=status)
    # Even a cursor just beyond a running Run cannot look past that barrier.
    with store.reading() as db:
        position = db.execute(
            "SELECT MAX(rowid) FROM runs WHERE conversation_id=?", (stream,)
        ).fetchone()[0]
    assert repo.preceding_source(stream, position + 1, 4096) is None


@pytest.mark.parametrize("limit", [0, 1, 4096])
def test_preceding_lookup_omits_whole_oversized_runs_instead_of_truncating(state, limit):
    store, stream, repo = state
    seed(store, stream, text="x" * 5000)
    seed(store, stream)
    current = repo.batch(stream, organizer(repo).policy).records[-1]
    assert repo.preceding_source(stream, current.position, limit) is None


async def test_preceding_run_respects_remaining_batch_bytes(state):
    store, stream, repo = state
    seed(store, stream, text="x" * 600)
    service = organizer(repo, page_runs=1)
    await service.organize(stream)
    current = seed(store, stream, text="y" * 600)
    provider = SegmentProvider()
    assert (await organizer(repo, provider, page_runs=1, source_bytes=1024).organize(stream))[
        "outcome"
    ] == "completed"
    data = json.loads(provider.requests[0][1].text)
    assert [run["run_id"] for run in data["runs"]] == [current]
    assert data["preceding_context"] is None


async def test_required_time_gap_omits_preceding_context(state):
    store, stream, repo = state
    stamp = datetime.now(UTC) - timedelta(days=1)
    seed(store, stream, at=stamp)
    provider = SegmentProvider()
    service = organizer(repo, provider, page_runs=1)
    await service.organize(stream)
    seed(store, stream, at=stamp + timedelta(hours=4))
    assert (await service.organize(stream))["outcome"] == "completed"
    assert json.loads(provider.requests[-1][1].text)["preceding_context"] is None


async def test_preceding_context_is_before_the_merged_provisional_sources(state):
    store, stream, repo = state
    stamp = datetime.now(UTC)
    first = seed(store, stream, at=stamp)
    provider = SegmentProvider()
    service = organizer(repo, provider, page_runs=3)
    # Finalize the first Episode without changing its actual source timestamp.
    await service.organize(stream, at=stamp + timedelta(hours=4))
    second = seed(store, stream, at=stamp + timedelta(seconds=60))
    await service.organize(stream, at=stamp + timedelta(seconds=120))
    third = seed(store, stream, at=stamp + timedelta(seconds=180))
    assert (await service.organize(stream, at=stamp + timedelta(seconds=240)))[
        "outcome"
    ] == "completed"
    data = json.loads(provider.requests[-1][1].text)
    assert [run["run_id"] for run in data["runs"]] == [second, third]
    assert data["preceding_context"]["run_id"] == first
    assert sorted(e.run_ids for e in repo.list(stream)) == sorted([[first], [second, third]])


@pytest.mark.parametrize("same_thread", [True, False])
async def test_preceding_context_respects_online_thread_ownership(state, same_thread):
    store, stream, repo = state
    routing = RoutingRepository(store, repo.agent_id, stream, "cli")
    first_thread = routing.create("插画")
    second_thread = first_thread if same_thread else routing.create("编曲")
    provider = SegmentProvider()
    service = organizer(repo, provider, page_runs=1)
    ids = []
    for thread in [first_thread, second_thread]:
        run = store.begin_run(stream, TextMessage(role="user", text="继续讨论。"))
        routing.bind(run, thread.id, "test")
        store.append_message(run, AssistantMessage(text="好的。"))
        store.finish_run(run, RunResult(status="completed", rounds=1))
        ids.append(run)
        assert (await service.organize(stream))["outcome"] == "completed"
    preceding = json.loads(provider.requests[-1][1].text)["preceding_context"]
    assert (preceding["run_id"] if preceding else None) == (ids[0] if same_thread else None)


def test_preceding_lookup_is_scoped_to_conversation_and_agent(state):
    store, stream, repo = state
    first = seed(store, stream)
    other = store.create_conversation(repo.agent_id)
    seed(store, other.id, text="另一个会话的内容。")
    current_id = seed(store, stream)
    current = repo.batch(stream, organizer(repo).policy).records[-1]
    assert current.run_id == current_id
    assert repo.preceding_source(stream, current.position, 4096).run_id == first
    assert repo.preceding_source(other.id, current.position - 1, 4096) is None
    with pytest.raises(HistoryError, match="inaccessible"):
        EpisodeRepository(store, "other-agent").preceding_source(stream, current.position + 1, 4096)
    with pytest.raises(HistoryError, match="inaccessible"):
        repo.preceding_source("other-conversation", current.position + 1, 4096)
