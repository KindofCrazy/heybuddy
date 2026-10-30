"""Regression coverage for cancellation evidence, maintenance fairness and Thread metadata."""

import pytest

from personal_agent.core.history import HistoryError
from personal_agent.core.models import AssistantMessage, RunResult, TextMessage
from personal_agent.organization.runtime import OrganizationRuntime
from personal_agent.proactive.delivery import DeliveryError, FakeSender
from personal_agent.proactive.models import ScoutResult
from tests.test_episodes import organizer, seed
from tests.test_episodes import state as journal_state
from tests.test_learning import learner, script
from tests.test_learning import state as learning_state
from tests.test_outbound import permission, pipeline, prepared
from tests.test_proactive import ProposalProvider, proactive
from tests.test_reflection import seed as priority_seed
from tests.test_threads import thread_service

__all__ = ["journal_state", "learning_state"]


async def test_short_cancellation_survives_large_assistant_and_provider_payload(journal_state):
    store, stream, _ = journal_state
    service, outbound = pipeline(store, stream)
    item = await prepared(store, stream, service, outbound)
    rid = store.begin_run(stream, TextMessage(role="user", text="取消测试，不要再提醒我"))
    store.append_message(
        rid, AssistantMessage(text="x" * 40000, provider_data={"test": [{"data": "y" * 40000}]})
    )
    store.finish_run(rid, RunResult(status="completed", rounds=1))
    snapshot = service.repository.snapshot(service.policy)
    assert snapshot["skip"] == ""
    assert [source.text for source in snapshot["users"]] == ["取消测试，不要再提醒我"]
    service.model.provider.status = "dismissed"
    await service.run()
    assert service.repository.candidates()[0]["status"] == "dismissed"
    sender = FakeSender()
    assert (await outbound.simulate(item["id"], sender, lambda: permission(stream)))[
        "outcome"
    ] == "stale"
    assert not sender.messages


async def test_unreadable_user_evidence_blocks_without_advancing_and_survives_restart(
    journal_state,
):
    store, stream, _ = journal_state
    service, outbound = pipeline(store, stream)
    item = await prepared(store, stream, service, outbound)
    before = service.repository.state()["run_cursor"]
    service.policy.source_bytes = 1024
    seed(store, stream, "取消测试，不要再提醒我。" + "x" * 2000)
    snapshot = service.repository.snapshot(service.policy)
    with pytest.raises(HistoryError):
        service.repository.publish(snapshot, ScoutResult(), service.policy, service.clock.now())
    calls = len(service.model.provider.requests)
    assert (await service.run()) == dict(outcome="blocked", error_code="source_budget")
    assert service.repository.state()["run_cursor"] == before
    assert service.gate() == "failed" and not service.ready()
    assert len(service.model.provider.requests) == calls
    assert proactive(store).gate() == "failed"
    sender = FakeSender()
    assert (await outbound.simulate(item["id"], sender, lambda: permission(stream)))[
        "outcome"
    ] == "stale"
    assert not sender.messages
    service.retry()
    assert (await service.run())["outcome"] == "blocked"
    service.policy.source_bytes = 32768
    service.retry()
    service.model.provider.status = "dismissed"
    assert (await service.run())["outcome"] == "completed"
    assert service.repository.state()["run_cursor"] > before
    assert service.repository.candidates()[0]["status"] == "dismissed"


@pytest.mark.parametrize("phase", ["attention", "prepared", "accepted", "uncertain"])
async def test_thread_metadata_keeps_semantic_revision_and_never_replays_terminal(
    phase, journal_state
):
    store, stream, repo = journal_state
    service, outbound = pipeline(store, stream)
    seed(store, stream, "明天测试 HeyBuddy 记忆模块")
    await service.run()
    await service.run()
    item = None
    sender = FakeSender()
    if phase != "attention":
        result = await service.run()
        item = outbound.repository.get(result["outbox_id"])
    if phase == "accepted":
        await outbound.simulate(item["id"], sender, lambda: permission(stream))
    elif phase == "uncertain":

        class Unknown(FakeSender):
            async def send(self, *args):
                raise DeliveryError()

        await outbound.simulate(item["id"], Unknown(), lambda: permission(stream))
    before = service.repository.candidates()[0]
    await organizer(repo).organize(stream)
    await thread_service(store).organize()
    await service.run()
    after = service.repository.candidates()[0]
    assert after["thread_id"] and after["revision"] == before["revision"]
    assert after["sources"] == before["sources"] and after["status"] == "open"
    assert any(
        event["action"] == "linked_thread"
        for event in service.repository.listing("decisions")["items"]
    )
    if phase == "attention":
        result = await service.run()
        assert result["stage"] == "composer"
        item = outbound.repository.get(result["outbox_id"])
    result = await outbound.simulate(item["id"], sender, lambda: permission(stream))
    if phase in {"accepted", "uncertain"}:
        assert result["outcome"] == "blocked"
        assert len(outbound.repository.listing("attempts")["items"]) == 1
    else:
        assert result["outcome"] == "accepted" and len(sender.messages) == 1
    # Actual new user evidence still changes the revision and closes the opportunity.
    seed(store, stream, "取消测试，不用再问")
    service.model.provider.status = "dismissed"
    await service.run()
    closed = service.repository.candidates()[0]
    assert closed["status"] == "dismissed" and closed["revision"] > after["revision"]


async def test_learning_and_proactive_alternate_with_default_whole_pipeline_budget(learning_state):
    store, sessions, _, _ = learning_state
    provider = script([])
    learning = learner(learning_state, provider)
    passive = ProposalProvider(mutate=lambda result, _: result.update(changes=[]))
    service = proactive(store, passive)
    for _ in range(4):
        priority_seed(store, sessions.current.id, "请记住：我叫陈宁")
    learning.poll()
    assert learning.ready(idle=True)
    runtime = OrganizationRuntime(
        organizer(learning.repository.episodes, enabled=False), learning=learning, proactive=service
    )
    try:
        await runtime._cycle(
            sessions.current.id, runtime.schedule_repository.state(sessions.current.id), True
        )
        assert provider.requests and not passive.requests
        assert runtime.last_report["cycle"]["requests"] <= 4
        # Keep Learning ready again while Proactive still has unread Runs.
        priority_seed(store, sessions.current.id, "请记住：我叫小陈")
        learning.poll()
        await runtime._cycle(
            sessions.current.id, runtime.schedule_repository.state(sessions.current.id), True
        )
        assert len(passive.requests) == 1
        provider.responses.extend(script([]).responses)
        await runtime._cycle(
            sessions.current.id, runtime.schedule_repository.state(sessions.current.id), True
        )
        assert len(provider.requests) == 2 and len(passive.requests) == 1
        assert runtime.last_report["cycle"]["requests"] <= 4
    finally:
        await runtime.close()


async def test_incompatible_learning_budget_does_not_block_scout(learning_state):
    store, sessions, _, _ = learning_state
    provider = script([])
    learning = learner(learning_state, provider)
    service = proactive(store)
    priority_seed(store, sessions.current.id, "请记住：我叫陈宁")
    learning.poll()
    runtime = OrganizationRuntime(
        organizer(learning.repository.episodes, enabled=False, maintenance_requests=1),
        learning=learning,
        proactive=service,
    )
    try:
        await runtime._cycle(
            sessions.current.id, runtime.schedule_repository.state(sessions.current.id), True
        )
        assert not provider.requests and len(service.model.provider.requests) == 1
        assert runtime.last_report["learning"]["error_code"] == "learning_budget_config"
    finally:
        await runtime.close()
