"""Fake-clock dual-speed learning shares the existing single maintenance task."""

import asyncio
from datetime import timedelta

import pytest

from personal_agent.learning.service import MemoryLearningService
from personal_agent.organization.runtime import OrganizationRuntime
from tests.fakes import FakeProvider
from tests.test_episodes import organizer
from tests.test_learning import human_write, learner, script, state
from tests.test_organization_schedule import FakeClock, pump
from tests.test_reflection import candidate, seed

__all__ = ["state"]


def use_clock(learning, clock):
    learning.clock = clock
    return learning


async def test_activation_watermark_does_not_silently_relearn_old_runs(state):
    learning = learner(state, FakeProvider())
    learning.poll()
    assert learning.repository.pending_job("priority") is None
    assert learning.repository.activation()["run_floor"] > 0


@pytest.mark.parametrize(
    "text,queued",
    [
        ("我是陈宁", True),
        ("我叫陈宁。", True),
        ("请叫我小陈", True),
        ("我的名字是陈宁", True),
        ("我叫什么名字？", False),
        ("今天看了一本书。", False),
    ],
)
async def test_self_introduction_is_priority_hint_without_episode_or_model(state, text, queued):
    db, sessions, _, _ = state
    provider = FakeProvider()
    learning = learner(state, provider)
    run = seed(db, sessions.current.id, text)
    learning.poll()
    pending = learning.repository.pending_job("priority")
    assert bool(pending) is queued
    if queued:
        assert [r["id"] for r in pending["sources"]["runs"]] == [run]
    assert not learning.repository.pending_episodes(3)
    assert not provider.requests


async def test_priority_then_batch_keeps_other_run_and_consolidates_no_op(state):
    db, sessions, memory, _ = state
    learning = learner(state, FakeProvider())
    run = seed(db, sessions.current.id)
    extra = seed(db, sessions.current.id, "用户正在开发 HeyBuddy P2。")
    learning.model.provider = script(
        [candidate(sessions.current.id, run, sequence=3)],
        [{"path": "human.md", "candidate_ids": [1]}],
        [human_write()],
    )
    assert (await learning.learn_priority())["updated_files"] == 1
    episodes = organizer(learning.repository.episodes)
    await episodes.organize(sessions.current.id)
    episode = learning.repository.episodes.list(sessions.current.id)[0]
    learning.repository.episodes.finalize(
        sessions.current.id, episodes.policy, at=episode.ended_at + timedelta(hours=4)
    )
    provider = script(
        [candidate(sessions.current.id, run, sequence=3)],
        [{"path": "human.md", "candidate_ids": [1]}],
        [{"action": "no_op", "reason": "Priority 已记录"}],
    )
    learning.model.provider = provider
    result = await learning.learn_batch(force=True)
    assert result["outcome"] == "completed" and result["updated_files"] == 0
    assert extra in provider.requests[0][1].text
    assert memory.read().blocks["human.md"].count("回答先给结论") == 1
    assert not learning.repository.pending_episodes(3)


async def test_single_finalized_episode_learns_after_six_hours_without_new_message(state):
    db, sessions, memory, _ = state
    clock = FakeClock()
    provider = script([])
    learning = use_clock(learner(state, provider, priority_enabled=False), clock)
    seed(db, sessions.current.id, "普通但有后续价值的交流")
    episodes = organizer(learning.repository.episodes)
    await episodes.organize(sessions.current.id, at=clock.now())
    episode = learning.repository.episodes.list(sessions.current.id)[0]
    learning.repository.episodes.finalize(
        sessions.current.id, episodes.policy, at=episode.ended_at + timedelta(hours=4)
    )
    runtime = OrganizationRuntime(episodes, clock=clock, learning=learning)
    try:
        runtime.schedule(sessions.current.id)
        await pump()
        assert not provider.requests
        # Runs use real timestamps; set the fake clock relative to the persisted source.
        clock.wall = episode.ended_at
        clock.advance(21601)
        await pump(lambda: bool(provider.requests))
        assert len(provider.requests) == 1
        assert not learning.repository.pending_episodes(3)
    finally:
        await runtime.close()


async def test_priority_is_background_preemptible_and_old_automation_not_run(state):
    db, sessions, memory, _ = state
    clock = FakeClock()
    entered = asyncio.Event()

    class Waiting:
        async def generate(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    class OldAutomation:
        class policy:
            mode = "suggest"

        async def run(self, *args):
            pytest.fail("P2 and old Automation both consumed the Run")

    learning = use_clock(learner(state, Waiting()), clock)
    seed(db, sessions.current.id)
    runtime = OrganizationRuntime(
        organizer(learning.repository.episodes),
        learning=learning,
        clock=clock,
        automation=OldAutomation(),
    )
    try:
        runtime.schedule(sessions.current.id, after_answer=True)
        await asyncio.wait_for(entered.wait(), 3)
        assert runtime.current_stage == "learning"
        await runtime.foreground()
        assert learning.repository.pending_job("priority")["state"] == "pending"
        assert memory.read().blocks["human.md"] == "# Human\n"
        # Reconstructing the service retains the independent pending source.
        reopened = MemoryLearningService(
            learning.repository, learning.reflection, learning.policy, clock
        )
        assert reopened.repository.pending_job("priority") is not None
    finally:
        await runtime.close()


async def test_empty_extraction_progress_and_provider_failure_are_not_infinite_retries(state):
    db, sessions, memory, _ = state
    from personal_agent.core.models import ModelResponse

    learning = learner(state, FakeProvider(ModelResponse(text="invalid")))
    run = seed(db, sessions.current.id)
    assert (await learning.learn_priority())["outcome"] == "failed"
    assert (await learning.learn_priority())["outcome"] == "no_learning_work"
    learning.model.provider = script([])
    assert (await learning.learn_priority(retry=True))["outcome"] == "completed"
    assert not learning.repository.pending_job("priority")
    with db._transaction() as connection:
        assert (
            connection.execute("SELECT status FROM runs WHERE id=?", (run,)).fetchone()[0]
            == "completed"
        )


async def test_shared_cycle_budget_services_learning_and_episode_backlog(state):
    from personal_agent.core.models import ModelResponse

    db, sessions, memory, _ = state
    clock = FakeClock()
    provider = FakeProvider(*(ModelResponse(text='{"candidates":[]}') for _ in range(5)))
    learning = use_clock(learner(state, provider), clock)
    for number in range(20):
        seed(db, sessions.current.id, "记住这一偏好" if number < 5 else "项目交流")
    episodes = organizer(learning.repository.episodes)
    runtime = OrganizationRuntime(episodes, learning=learning, clock=clock)
    try:
        runtime.schedule(sessions.current.id)
        await pump(lambda: bool(runtime.last_report.get("cycle")))
        assert provider.requests
        assert episodes.repository.status(sessions.current.id)["position"] > 0
        assert runtime.last_report["cycle"]["requests"] <= 4
        assert runtime.last_report["cycle"]["attempted_batches"] <= 4
        assert runtime.diagnostics(sessions.current.id)["learning"]["counts"]
    finally:
        await runtime.close()


async def test_incompatible_shared_budget_never_repeats_paid_extraction(state):
    db, sessions, memory, _ = state
    clock = FakeClock()
    provider = script([])
    learning = use_clock(learner(state, provider), clock)
    seed(db, sessions.current.id)
    runtime = OrganizationRuntime(
        organizer(learning.repository.episodes, maintenance_requests=1),
        learning=learning,
        clock=clock,
    )
    try:
        runtime.schedule(sessions.current.id)
        await pump()
        clock.advance(3600)
        await pump()
        assert not provider.requests
        assert runtime.last_report["learning"]["error_code"] == "learning_budget_config"
        assert learning.repository.pending_job("priority") is not None
    finally:
        await runtime.close()


async def test_continuous_dirty_pages_yield_a_whole_cycle_to_waiting_learning(state):
    from types import SimpleNamespace

    from personal_agent.core.models import ModelResponse
    from personal_agent.organization.models import OrganizationPolicy

    db, sessions, memory, _ = state
    clock = FakeClock()
    provider = script([])
    learning = use_clock(learner(state, provider), clock)
    run = seed(db, sessions.current.id)
    with db._transaction() as connection:
        connection.execute(
            "UPDATE runs SET started_at=?,ended_at=? WHERE id=?",
            (
                (clock.now() - timedelta(hours=1, seconds=1)).isoformat(),
                (clock.now() - timedelta(hours=1)).isoformat(),
                run,
            ),
        )
    calls = []

    class RepairProvider:
        async def generate(self, *args, **kwargs):
            return ModelResponse(text="{}")

    model = SimpleNamespace(provider=RepairProvider())

    async def repair(**kwargs):
        calls.append(True)
        await model.provider.generate([], [], max_output_tokens=1)
        return {"outcome": "repair_page"}

    threads = SimpleNamespace(
        model=model,
        policy=OrganizationPolicy(),
        repair=repair,
        repairs=SimpleNamespace(pending=lambda **kwargs: object()),
        repository=SimpleNamespace(status=lambda: dict(pending_episodes_capped=0, state="pending")),
    )
    runtime = OrganizationRuntime(
        organizer(learning.repository.episodes), learning=learning, threads=threads, clock=clock
    )
    try:
        runtime.schedule(sessions.current.id)
        await pump(lambda: bool(runtime.last_report.get("cycle")))
        assert calls and not provider.requests
        clock.advance(61)
        await pump(lambda: bool(provider.requests))
        assert len(provider.requests) == 1
    finally:
        await runtime.close()
