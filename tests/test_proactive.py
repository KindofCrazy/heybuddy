"""Offline contracts: provenance, incremental progress, cancellation and shadow isolation."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from personal_agent.config import Settings, load_settings
from personal_agent.core.budget import ContextBudget
from personal_agent.core.history import HistoryError
from personal_agent.core.models import ModelResponse
from personal_agent.organization.models import OrganizationPolicy
from personal_agent.organization.provider import OrganizerModel
from personal_agent.organization.runtime import OrganizationRuntime
from personal_agent.proactive.models import ProactivePolicy, ScoutResult
from personal_agent.proactive.runtime import ProactiveRuntime
from personal_agent.runtime import assemble_chat
from personal_agent.storage import sqlite as storage
from personal_agent.storage.proactive import ProactiveRepository
from personal_agent.storage.sqlite import SQLiteStore
from tests.fakes import FakeProvider
from tests.test_dashboard import observatory
from tests.test_episodes import organizer, seed, state
from tests.test_threads import thread_service

__all__ = ["observatory", "state"]


class Clock:
    def __init__(self):
        self.time = datetime.now(UTC) + timedelta(seconds=5)

    def now(self):
        return self.time

    def monotonic(self):
        return 0

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class ProposalProvider(FakeProvider):
    def __init__(self, *, status="open", action="notify", mutate=None):
        super().__init__()
        self.status, self.action, self.mutate = status, action, mutate

    async def generate(self, messages, tools, *, max_output_tokens=None):
        data = json.loads(messages[1].text)
        if "users" in data:
            source = data["users"][0]
            result = dict(
                changes=[
                    dict(
                        topic_key="heybuddy-testing",
                        title="测试 HeyBuddy",
                        context="等待实际测试结果。",
                        status=self.status,
                        next_review_at=data["now"],
                        sources=[
                            dict(
                                conversation_id=source["conversation_id"],
                                run_id=source["run_id"],
                                sequence=source["sequence"],
                                quote=source["text"][:100],
                            )
                        ],
                    )
                ]
            )
        else:
            result = dict(
                action=self.action,
                proposal_ids=[data["candidates"][0]["id"]],
                reason="有真实计划，可以适当关注。",
                next_review_at=data["now"],
            )
        if self.mutate:
            self.mutate(result, data)
        self.responses.append(ModelResponse(text=json.dumps(result, ensure_ascii=False)))
        return await super().generate(messages, tools, max_output_tokens=max_output_tokens)


def proactive(store, provider=None, **changes):
    repository = ProactiveRepository(store, "heybuddy")
    policy = ProactivePolicy(enabled=True, quiet_start_hour=0, quiet_end_hour=0, **changes)
    return ProactiveRuntime(
        repository,
        OrganizerModel(
            provider or ProposalProvider(),
            ContextBudget(context_window=131072),
            OrganizationPolicy(
                input_tokens=policy.input_tokens, output_tokens=policy.output_tokens
            ),
        ),
        policy,
        clock=Clock(),
    )


async def test_no_backfill_incremental_restart_and_no_duplicate_send(state):
    store, stream, _ = state
    seed(store, stream, "旧历史不默认提取")
    service = proactive(store)
    assert not service.repository.pending()
    rid = seed(store, stream, "我明天准备测试 HeyBuddy 的记忆模块")
    assert (await service.run())["stage"] == "scout"
    first = service.repository.candidates()[0]
    assert first["sources"][0]["run_id"] == rid
    restarted = proactive(store)
    assert not restarted.repository.pending()
    assert (await restarted.run())["stage"] == "attention"
    assert not restarted.repository.due(restarted.clock.now(), 4)
    assert (await restarted.run())["outcome"] == "no_proactive_work"
    assert restarted.repository.candidates()[0]["status"] == "open"
    decisions = restarted.repository.listing("decisions")["items"]
    assert decisions[0]["action"] == "notify" and decisions[0]["payload"]["mode"] == "shadow"
    assert len(store.list_runs(stream)) == 2
    assert restarted.model.provider.tools == [[]]


async def test_empty_result_advances_and_foreign_agent_never_read(state):
    store, stream, _ = state
    provider = ProposalProvider(mutate=lambda result, _: result.update(changes=[]))
    service = proactive(store, provider)
    other = store.create_agent("other", "prompt")
    conversation = store.create_conversation(other.id)
    seed(store, conversation.id, "foreign secret")
    seed(store, stream, "今天吃饭很好吃")
    assert (await service.run())["changes"] == 0
    assert not service.repository.pending()
    assert "foreign secret" not in provider.requests[0][1].text
    assert not service.repository.candidates()


@pytest.mark.parametrize("invalid", ["quote", "run", "sequence", "duplicate"])
async def test_forged_sources_block_without_advancing_and_require_retry(state, invalid):
    store, stream, _ = state

    def mutate(result, data):
        ref = result["changes"][0]["sources"][0]
        if invalid == "quote":
            ref["quote"] = "这是建议，尚待确认。"  # Assistant-only text is not user evidence.
        elif invalid == "run":
            ref["run_id"] = "foreign"
        elif invalid == "sequence":
            ref["sequence"] += 1
        else:
            result["changes"].append(result["changes"][0])

    provider = ProposalProvider(mutate=mutate)
    service = proactive(store, provider)
    seed(store, stream, "明天测试 HeyBuddy")
    before = service.repository.state()
    assert (await service.run())["outcome"] == "failed"
    assert service.repository.state()["run_cursor"] == before["run_cursor"]
    assert not service.repository.candidates()
    assert (await service.run())["gate"] == "failed"
    assert len(provider.requests) == 1
    provider.mutate = None
    service.retry()
    assert (await service.run())["outcome"] == "completed"


async def test_latest_cancellation_beats_old_thread_and_repeated_runs_deduplicate(state):
    store, stream, repo = state
    service = proactive(store)
    seed(store, stream, "明天测试 HeyBuddy")
    await service.run()
    snapshot = service.repository.candidates()[0]
    seed(store, stream, "HeyBuddy 测试取消了，不用再问")
    service.model.provider.status = "dismissed"
    await service.run()
    closed = service.repository.candidates()[0]
    assert closed["id"] == snapshot["id"] and closed["revision"] == 2
    assert closed["status"] == "dismissed"
    await organizer(repo).organize(stream)
    threads = thread_service(store)
    await threads.organize()
    service.model.provider.status = "open"
    await service.run()
    value = service.repository.candidates()[0]
    assert value["status"] == "dismissed" and value["thread_id"] is not None
    assert len(service.repository.candidates()) == 1


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
async def test_uncompleted_runs_are_skipped_with_audit(state, status):
    store, stream, _ = state
    service = proactive(store)
    seed(store, stream, "不应成为证据", status=status)
    assert (await service.run())["changes"] == 0
    assert not service.model.provider.requests
    assert not service.repository.pending()
    assert service.repository.listing("decisions")["items"][0]["reason"] == "run_not_completed"


async def test_running_run_is_barrier_and_oversized_source_is_explicit_skip(state):
    store, stream, _ = state
    service = proactive(store, source_bytes=1024)
    rid = seed(store, stream, "running", status="running")
    assert service.gate() == "foreground"
    assert (await service.run())["outcome"] == "no_proactive_work"
    store.interrupt_run(rid)
    await service.run()
    seed(store, stream, "长内容" * 1000)
    await service.run()
    assert service.repository.listing("decisions")["items"][0]["reason"] == "source_budget"
    assert not service.model.provider.requests


@pytest.mark.parametrize("action", ["silent", "notify", "propose_task", "defer"])
async def test_attention_lifecycle_and_cooldown(state, action):
    store, stream, _ = state
    service = proactive(store, ProposalProvider(action=action))
    seed(store, stream, "明天测试 HeyBuddy")
    await service.run()
    assert (await service.run())["outcome"] == "completed"
    item = service.repository.candidates()[0]
    assert item["last_action"] == action
    if action == "defer":
        assert item["status"] == "deferred"
        assert datetime.fromisoformat(item["next_review_at"]) >= (
            service.clock.now() + timedelta(seconds=service.policy.review_seconds)
        )
    else:
        assert item["next_review_at"] is None and item["status"] == "open"
    assert (await service.run())["outcome"] == "no_proactive_work"
    assert len(store.list_runs(stream)) == 1


async def test_quiet_hours_and_expiry_never_call_attention(state):
    store, stream, _ = state
    service = proactive(store)
    seed(store, stream, "明天测试 HeyBuddy")
    await service.run()
    service.policy.quiet_start_hour = 23
    service.policy.quiet_end_hour = 8
    service.clock.time = service.clock.time.replace(hour=16)  # UTC+8 midnight.
    assert service.gate() == "quiet_hours"
    assert (await service.run())["outcome"] == "no_proactive_work"
    assert len(service.model.provider.requests) == 1
    service.clock.time += timedelta(days=8)
    await service.run()
    assert service.repository.candidates()[0]["status"] == "expired"


async def test_new_input_during_attention_invalidates_decision(state):
    store, stream, _ = state
    service = proactive(store)
    seed(store, stream, "明天测试 HeyBuddy")
    await service.run()

    def mutate(result, data):
        seed(store, stream, "取消了，不要再提")

    service.model.provider.mutate = mutate
    assert (await service.run())["outcome"] == "stale"
    assert service.repository.candidates()[0]["last_action"] == ""
    assert not any(
        d["action"] == "notify" for d in service.repository.listing("decisions")["items"]
    )


async def test_qq_receipt_epoch_blocks_decision_before_run_exists(state):
    store, stream, _ = state
    service = proactive(store)
    seed(store, stream, "明天测试 HeyBuddy")
    await service.run()
    service.model.provider.mutate = lambda *_: setattr(service, "activity_epoch", 1)
    assert (await service.run())["outcome"] == "stale"


async def test_malformed_attention_not_retried_without_explicit_command(state):
    store, stream, _ = state
    service = proactive(store)
    seed(store, stream, "明天测试 HeyBuddy")
    await service.run()
    service.model.provider.mutate = lambda result, _: result.update(proposal_ids=["foreign"])
    assert (await service.run())["outcome"] == "failed"
    assert service.repository.candidates()[0]["last_action"] == "failed"
    assert (await service.run())["outcome"] == "no_proactive_work"
    service.retry()
    service.model.provider.mutate = None
    assert (await service.run())["outcome"] == "completed"


async def test_cancelled_scout_never_publishes_even_if_provider_swallows_cancel(state):
    store, stream, _ = state
    entered = asyncio.Event()

    class Slow(ProposalProvider):
        async def generate(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return await super().generate(*args, **kwargs)

    service = proactive(store, Slow())
    seed(store, stream, "明天测试 HeyBuddy")
    task = asyncio.create_task(service.run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not service.repository.candidates()
    assert service.repository.state()["run_cursor"] == 0


async def test_proactive_and_organization_share_one_maintenance_budget(state):
    store, stream, repo = state
    service = proactive(store)
    seed(store, stream, "明天测试 HeyBuddy")
    runtime = OrganizationRuntime(organizer(repo, maintenance_requests=1), proactive=service)
    await runtime._cycle(stream, runtime.schedule_repository.state(stream), True)
    assert runtime.last_report["cycle"]["requests"] == 1
    assert len(service.model.provider.requests) == 1
    assert runtime.episodes.repository.list(stream) == []
    assert service.model.provider.__class__ is ProposalProvider
    await runtime.close()


async def test_proactive_works_when_p1_p2_p3_disabled_and_timer_preempts(state):
    store, stream, repo = state
    service = proactive(store)
    seed(store, stream, "明天测试 HeyBuddy")
    runtime = OrganizationRuntime(
        organizer(
            repo,
            enabled=False,
            idle_seconds=0.01,
            check_seconds=0.01,
            cooldown_seconds=0.01,
        ),
        proactive=service,
    )
    runtime.schedule(stream)
    try:
        for _ in range(100):
            if service.repository.candidates():
                break
            await asyncio.sleep(0.01)
        assert service.repository.candidates()
        await runtime.foreground()
        assert runtime.task is None
    finally:
        await runtime.close()


def test_atomic_migration_15_to_16_and_progress_cas(tmp_path, monkeypatch):
    path = tmp_path / "upgrade.db"
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 15)
        with SQLiteStore(path) as store:
            store.initialize()
            from personal_agent.sessions import SessionManager

            sessions = SessionManager(store)
            seed(store, sessions.current.id, "历史保留")
    with SQLiteStore(path) as store:
        store.initialize()
        assert (
            store._connection.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
        )
        assert len(store.list_runs(sessions.current.id)) == 1
        service = proactive(store)
        seed(store, sessions.current.id, "明天测试 HeyBuddy")
        snapshot = service.repository.snapshot(service.policy)
        service.repository.publish(snapshot, ScoutResult(), service.policy, service.clock.now())
        with pytest.raises(HistoryError):
            service.repository.publish(snapshot, ScoutResult(), service.policy, service.clock.now())


async def test_dashboard_proactive_read_only_pagination_and_agent_scope(observatory):
    client, store, chat, provider, _ = observatory
    service = proactive(store)
    chat.organization.proactive = service
    seed(store, chat.sessions.current.id, "明天测试 HeyBuddy")
    await service.run()
    await service.run()
    other = store.create_agent("other", "prompt")
    foreign = ProactiveRepository(store, other.id)
    foreign.initialize()
    foreign.fail("foreign_error", datetime.now(UTC))
    before = store._connection.total_changes
    for path in [
        "/proactive",
        "/api/proactive/status",
        "/api/proactive/proposals",
        "/api/proactive/decisions?limit=1",
    ]:
        response = await client.get(path)
        assert response.status_code == 200
        assert "foreign_error" not in response.text
    first = (await client.get("/api/proactive/decisions?limit=1")).json()
    second = (
        await client.get(f"/api/proactive/decisions?limit=1&cursor={first['next_cursor']}")
    ).json()
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert (await client.post("/api/proactive/proposals")).status_code == 405
    assert (await client.get("/api/proactive/proposals?limit=100")).status_code == 422
    assert store._connection.total_changes == before
    assert not provider.requests


def test_shadow_defaults_on_environment_can_disable_and_observer_never_initializes(state, tmp_path):
    store, _, _ = state
    from personal_agent.sessions import SessionManager

    settings = Settings(api_key="synthetic", model="synthetic", context_window=131072)
    assert settings.proactive_enabled is True
    assert ProactivePolicy().enabled is True
    path = tmp_path / "synthetic.env"
    path.write_text(
        "OPENAI_API_KEY=synthetic\nOPENAI_MODEL=synthetic\n"
        "AGENT_CONTEXT_WINDOW=131072\nAGENT_PROACTIVE_ENABLED=true\n",
        encoding="utf-8",
    )
    settings = load_settings(path)
    assert settings.proactive_policy().enabled
    # Reuse an already initialized service's memory through a separate observer assembly.
    from personal_agent.memory.store import MemoryFS

    sessions = SessionManager(store)
    memory = MemoryFS.for_agent(tmp_path / "memory", sessions.agent.id)
    before = store._connection.total_changes
    observer = assemble_chat(settings, sessions, memory, FakeProvider(), observer_only=True)
    assert observer.organization.proactive.repository.state() is None
    assert observer.organization.proactive.status()["gate"] == "uninitialized"
    assert store._connection.total_changes == before
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "AGENT_PROACTIVE_ENABLED=true", "AGENT_PROACTIVE_ENABLED=false"
        ),
        encoding="utf-8",
    )
    assert load_settings(path).proactive_enabled is False


async def test_file_sql_failure_rolls_back_opportunity_and_cursor(state):
    store, stream, _ = state
    service = proactive(store)
    seed(store, stream, "明天测试 HeyBuddy")
    with store._transaction() as db:
        db.execute(
            "CREATE TRIGGER fail_audit BEFORE INSERT ON proactive_decisions "
            "WHEN NEW.action='scouted' BEGIN SELECT RAISE(ABORT,'test'); END"
        )
    assert (await service.run())["error_code"] == "publication_failed"
    assert service.repository.state()["run_cursor"] == 0
    assert not service.repository.candidates()
    assert service.repository.state()["error_code"] == "publication_failed"


def test_migration_16_failure_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "rollback.db"
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 15)
        with SQLiteStore(path) as store:
            store.initialize()
    with monkeypatch.context() as patch:
        patch.setitem(storage.MIGRATIONS, 16, (*storage.MIGRATIONS[16], "INVALID SQL"))
        with SQLiteStore(path) as store:
            with pytest.raises(HistoryError):
                store.initialize()
            assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 15
            assert (
                store._connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='proactive_state'"
                ).fetchone()
                is None
            )


async def test_thread_can_discover_opportunity_but_must_cite_raw_user(state):
    store, stream, repo = state
    service = proactive(store, ProposalProvider(mutate=lambda result, _: result.update(changes=[])))
    seed(store, stream, "明天测试 HeyBuddy")
    await service.run()
    assert not service.repository.candidates()
    await organizer(repo).organize(stream)
    threads = thread_service(store)
    await threads.organize()
    service.model.provider.mutate = None
    assert (await service.run())["changes"] == 1
    proposal = service.repository.candidates()[0]
    assert proposal["thread_id"] == threads.repository.list()[0].id
    data = json.loads(service.model.provider.requests[-1][1].text)
    assert data["thread_hint"] and data["users"]


async def test_foreground_cancels_proactive_through_shared_runtime(state):
    store, stream, repo = state
    entered = asyncio.Event()

    class Slow:
        async def generate(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    service = proactive(store, Slow())
    seed(store, stream, "明天测试 HeyBuddy")
    runtime = OrganizationRuntime(organizer(repo, idle_seconds=0.01), proactive=service)
    runtime.schedule(stream)
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(runtime.foreground(), 1)
    assert service.repository.state()["run_cursor"] == 0
    assert not service.repository.candidates()
    await runtime.close()


async def test_revision_change_during_attention_invalidates_decision(state):
    store, stream, _ = state
    service = proactive(store)
    seed(store, stream, "明天测试 HeyBuddy")
    await service.run()

    def change_revision(*_):
        with store._transaction() as db:
            db.execute("UPDATE proactive_proposals SET revision=revision+1,status='dismissed'")

    service.model.provider.mutate = change_revision
    assert (await service.run())["outcome"] == "stale"
    assert service.repository.candidates()[0]["status"] == "dismissed"


async def test_expiry_remains_ready_during_quiet_hours(state):
    store, stream, _ = state
    service = proactive(store)
    seed(store, stream, "明天测试 HeyBuddy")
    await service.run()
    service.policy.quiet_start_hour, service.policy.quiet_end_hour = 23, 8
    service.clock.time += timedelta(days=8)
    service.clock.time = service.clock.time.replace(hour=16)
    assert service.ready()
    assert (await service.run())["stage"] == "expiry"
    assert not service.ready()


async def test_capacity_skip_advances_so_later_cancellation_can_still_close(state):
    store, stream, _ = state
    service = proactive(store)
    seed(store, stream, "明天测试 HeyBuddy")
    await service.run()
    first = service.repository.candidates()[0]
    with store._transaction() as db:
        for index in range(47):
            db.execute(
                "INSERT INTO proactive_proposals "
                "(id,agent_id,topic_key,title,context,sources,source_position,status,revision,"
                "next_review_at,expires_at,updated_at) VALUES (?,?,?,?,?,?,?,'open',1,?,?,?)",
                (
                    f"capacity-{index}",
                    service.repository.agent_id,
                    f"capacity-{index}",
                    first["title"],
                    first["context"],
                    json.dumps(first["sources"]),
                    first["source_position"],
                    first["next_review_at"],
                    first["expires_at"],
                    first["updated_at"],
                ),
            )
    seed(store, stream, "我明天还有另一个实验计划")
    service.model.provider.mutate = lambda result, _: result["changes"][0].update(
        topic_key="new-item"
    )
    assert (await service.run())["outcome"] == "completed"
    assert not service.repository.pending()
    assert len(service.repository.candidates()) == 48
    assert any(
        item["reason"] == "active_capacity"
        for item in service.repository.listing("decisions")["items"]
    )
    service.model.provider.mutate = None
    service.model.provider.status = "dismissed"
    seed(store, stream, "HeyBuddy 测试取消，不要再问")
    await service.run()
    assert service.repository.candidates()[-1]["status"] == "dismissed"


async def test_expiring_during_attention_is_rejected_at_final_host_check(state):
    store, stream, _ = state
    service = proactive(store)
    seed(store, stream, "明天测试 HeyBuddy")
    await service.run()
    service.model.provider.mutate = lambda *_: setattr(
        service.clock, "time", service.clock.now() + timedelta(days=8)
    )
    assert (await service.run())["outcome"] == "stale"
    assert service.repository.candidates()[0]["last_action"] == ""
