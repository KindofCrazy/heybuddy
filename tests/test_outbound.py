"""End-to-end local simulation contracts, with no external models or real senders."""

import asyncio
import json

import pytest

from personal_agent.character.context import CharacterContextCompiler
from personal_agent.character.loader import CharacterLoader
from personal_agent.cli_commands import MaintenanceCommands
from personal_agent.core.agent import Agent
from personal_agent.core.history import HistoryError
from personal_agent.core.models import ModelResponse
from personal_agent.proactive.delivery import DeliveryError, DeliveryGrant, FakeSender, SendReceipt
from personal_agent.proactive.outbound import OutboundRuntime
from personal_agent.sessions import SessionManager
from personal_agent.storage import sqlite as storage
from personal_agent.storage.outbox import OutboundContext, OutboxRepository
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.tools.builtin import create_builtin_registry
from tests.fakes import FakeProvider
from tests.test_dashboard import observatory
from tests.test_episodes import seed, state
from tests.test_proactive import ProposalProvider, proactive

__all__ = ["state", "observatory"]
TEXT = "你之前说要测试记忆模块，后来跑得怎么样？"


class PipelineProvider(ProposalProvider):
    def __init__(self):
        super().__init__()
        self.draft_mutate = None

    async def generate(self, messages, tools, *, max_output_tokens=None):
        data = json.loads(messages[1].text)
        if "persona_style" not in data:
            return await super().generate(messages, tools, max_output_tokens=max_output_tokens)
        value = dict(text=TEXT)
        if self.draft_mutate:
            self.draft_mutate(value, data)
        self.responses.append(ModelResponse(text=json.dumps(value, ensure_ascii=False)))
        return await FakeProvider.generate(
            self, messages, tools, max_output_tokens=max_output_tokens
        )


def pipeline(store, stream):
    service = proactive(store, PipelineProvider())
    character = CharacterContextCompiler(CharacterLoader().load("luna"))
    outbox = OutboxRepository(store, service.repository.agent_id)
    outbound = OutboundRuntime(
        service,
        outbox,
        lambda: stream,
        lambda: (
            character.package.fingerprint,
            [dict(role=m.role, text=m.text) for m in character.compile("")],
        ),
    )
    service.outbound = outbound
    return service, outbound


def permission(stream, **changes):
    return DeliveryGrant(
        enabled=True, conversation_id=stream, recipient="simulation:" + stream, **changes
    )


async def prepared(store, stream, service, outbound):
    seed(store, stream, "明天我要测试 HeyBuddy 记忆模块")
    assert (await service.run())["stage"] == "scout"
    assert (await service.run())["stage"] == "attention"
    result = await service.run()
    assert result["stage"] == "composer" and result["mode"] == "preview"
    return outbound.repository.get(result["outbox_id"])


async def test_complete_pipeline_persona_draft_explicit_simulation_and_dedupe(state):
    store, stream, _ = state
    service, outbound = pipeline(store, stream)
    item = await prepared(store, stream, service, outbound)
    assert item["text"] == TEXT and item["status"] == "prepared"
    provider = service.model.provider
    payload = json.loads(provider.requests[-1][1].text)
    assert any("Luna" in m["text"] for m in payload["persona_style"])
    assert payload["proposal"]["sources"][0]["quote"] == "明天我要测试 HeyBuddy 记忆模块"
    assert provider.tools == [[], [], []]
    sender = FakeSender()
    grant = permission(stream)
    assert (await outbound.simulate(item["id"], sender, lambda: grant))["outcome"] == "accepted"
    accepted = outbound.repository.get(item["id"])
    assert accepted["mode"] == "simulation" and accepted["receipt_id"] == item["id"]
    assert (await outbound.simulate(item["id"], sender, lambda: grant))["outcome"] == "blocked"
    assert len(sender.messages) == 1 and len(outbound.repository.listing("attempts")["items"]) == 1
    assert service.repository.candidates()[0]["status"] == "open"
    assert (await service.run())["outcome"] == "no_proactive_work"
    assert len(store.list_runs(stream)) == 1


async def test_fake_notices_only_in_explicit_harness_never_journal_or_learning(state):
    store, stream, _ = state
    service, outbound = pipeline(store, stream)
    item = await prepared(store, stream, service, outbound)
    sender = FakeSender()
    await outbound.simulate(item["id"], sender, lambda: permission(stream))
    assert OutboundContext(outbound.repository, stream).context() is None
    sessions = SessionManager(store)
    sessions.notices = OutboundContext(outbound.repository, stream, include_simulations=True)
    provider = FakeProvider(ModelResponse(text="那就先看已经跑完的部分。"))
    context = sessions.context()
    agent = Agent(provider, create_builtin_registry(), context=context)
    result = await agent.run("还没做完")
    assert result.status == "completed"
    packets = [
        json.loads(m.text) for m in provider.requests[0] if '"kind": "proactive_messages"' in m.text
    ]
    assert len(packets) == 1
    assert packets[0]["origin"] == "proactive" and packets[0]["user_evidence"] is False
    assert packets[0]["simulation"] is True and packets[0]["messages"][0]["text"] == TEXT
    raw = [m.content.text for m in store.load_messages(stream)]
    assert TEXT not in raw and raw[-2] == "还没做完"
    from personal_agent.recall.service import Recall

    assert not Recall(store, "heybuddy").search("后来跑得怎么样").hits
    sources = service.repository.snapshot(service.policy)["users"]
    assert [s.text for s in sources] == ["还没做完"]
    other = store.create_conversation(sessions.agent.id)
    assert (
        OutboundContext(outbound.repository, other.id, include_simulations=True).context() is None
    )


@pytest.mark.parametrize(
    "change", ["epoch", "input", "proposal", "conversation", "expiry", "persona"]
)
async def test_composer_result_cannot_publish_after_host_state_changes(state, change):
    store, stream, _ = state
    service, outbound = pipeline(store, stream)
    seed(store, stream, "明天测试 HeyBuddy")
    await service.run()
    await service.run()

    def mutate(value, data):
        if change == "epoch":
            service.activity_epoch += 1
        elif change == "input":
            seed(store, stream, "取消了")
        elif change == "proposal":
            with store._transaction() as db:
                db.execute("UPDATE proactive_proposals SET status='dismissed',revision=revision+1")
        elif change == "conversation":
            outbound.conversation = lambda: "new-conversation"
        elif change == "expiry":
            from datetime import timedelta

            service.clock.time += timedelta(days=8)
        else:
            outbound.persona = lambda: ("changed", [])

    service.model.provider.draft_mutate = mutate
    result = await service.run()
    assert result["outcome"] == "stale"
    assert outbound.repository.get(result["outbox_id"])["status"] == "cancelled"


@pytest.mark.parametrize("cause", ["disabled", "foreign_scope", "revoked", "live_sender", "input"])
async def test_permission_and_latest_input_rechecked_before_sender(state, cause):
    store, stream, _ = state
    service, outbound = pipeline(store, stream)
    item = await prepared(store, stream, service, outbound)
    sender = FakeSender()
    grant = permission(stream)
    calls = 0

    def current():
        nonlocal calls
        calls += 1
        return (
            grant.model_copy(update={"revision": 2}) if cause == "revoked" and calls > 1 else grant
        )

    if cause == "disabled":
        grant = grant.model_copy(update={"enabled": False})
    elif cause == "foreign_scope":
        grant = grant.model_copy(update={"conversation_id": "foreign"})
    elif cause == "live_sender":
        sender.mode = "live"
    elif cause == "input":
        service.activity_epoch += 1
    assert (await outbound.simulate(item["id"], sender, current))["outcome"] in {"blocked", "stale"}
    assert not sender.messages and not outbound.repository.listing("attempts")["items"]


@pytest.mark.parametrize("uncertain", [True, False])
async def test_failed_or_uncertain_attempt_is_never_automatically_retried(state, uncertain):
    store, stream, _ = state
    service, outbound = pipeline(store, stream)
    item = await prepared(store, stream, service, outbound)

    class Broken(FakeSender):
        async def send(self, *args):
            raise DeliveryError(uncertain=uncertain)

    sender = Broken()
    result = await outbound.simulate(item["id"], sender, lambda: permission(stream))
    assert result["outcome"] == ("uncertain" if uncertain else "failed")
    outbound.repository.recover(service.clock.now())
    assert (await outbound.simulate(item["id"], sender, lambda: permission(stream)))[
        "outcome"
    ] == "blocked"
    assert len(outbound.repository.listing("attempts")["items"]) == 1


async def test_cancellation_after_effect_is_uncertain_and_not_replayed(state):
    store, stream, _ = state
    service, outbound = pipeline(store, stream)
    item = await prepared(store, stream, service, outbound)
    entered = asyncio.Event()

    class Slow(FakeSender):
        async def send(self, *args):
            await super().send(*args)
            entered.set()
            await asyncio.Event().wait()

    sender = Slow()
    task = asyncio.create_task(outbound.simulate(item["id"], sender, lambda: permission(stream)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(sender.messages) == 1
    assert outbound.repository.get(item["id"])["status"] == "uncertain"


async def test_sender_swallowing_timeout_does_not_get_accepted_receipt(state):
    store, stream, _ = state
    service, outbound = pipeline(store, stream)
    item = await prepared(store, stream, service, outbound)
    service.policy.timeout_seconds = 0.01

    class Late(FakeSender):
        async def send(self, *args):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return SendReceipt(receipt_id="late")

    assert (await outbound.simulate(item["id"], Late(), lambda: permission(stream)))[
        "outcome"
    ] == "uncertain"


async def test_ack_database_failure_preserves_effect_and_recovers_uncertain(state, monkeypatch):
    store, stream, _ = state
    service, outbound = pipeline(store, stream)
    item = await prepared(store, stream, service, outbound)
    sender = FakeSender()

    def fail(*args, **kwargs):
        raise HistoryError("synthetic failure")

    with monkeypatch.context() as patch:
        patch.setattr(outbound.repository, "finish", fail)
        with pytest.raises(HistoryError):
            await outbound.simulate(item["id"], sender, lambda: permission(stream))
    assert len(sender.messages) == 1 and outbound.repository.get(item["id"])["status"] == "sending"
    outbound.repository.recover(service.clock.now())
    assert outbound.repository.get(item["id"])["status"] == "uncertain"
    assert (await outbound.simulate(item["id"], sender, lambda: permission(stream)))[
        "outcome"
    ] == "blocked"


async def test_attempt_commit_failure_prevents_sender_effect(state):
    store, stream, _ = state
    service, outbound = pipeline(store, stream)
    item = await prepared(store, stream, service, outbound)
    with store._transaction() as db:
        db.execute(
            "CREATE TRIGGER fail_attempt BEFORE INSERT ON proactive_attempts "
            "BEGIN SELECT RAISE(ABORT,'test'); END"
        )
    sender = FakeSender()
    with pytest.raises(HistoryError):
        await outbound.simulate(item["id"], sender, lambda: permission(stream))
    assert not sender.messages and outbound.repository.get(item["id"])["status"] == "prepared"


async def test_recovery_freezes_prepared_and_explicit_retry_regenerates_failed_draft(state):
    store, stream, _ = state
    service, outbound = pipeline(store, stream)
    seed(store, stream, "明天测试 HeyBuddy")
    await service.run()
    await service.run()
    service.model.provider.draft_mutate = lambda value, _: value.update(text=" ")
    result = await service.run()
    assert result["outcome"] == "failed"
    item_id = result["outbox_id"]
    assert not outbound.ready()
    service.model.provider.draft_mutate = None
    assert (await outbound.compose(item_id))["outcome"] == "completed"
    outbound.repository.recover(service.clock.now())
    assert outbound.repository.get(item_id)["status"] == "cancelled"
    assert not outbound.ready()


async def test_recipient_rate_limit_survives_recreation(state):
    store, stream, _ = state
    service, outbound = pipeline(store, stream)
    first = await prepared(store, stream, service, outbound)
    sender, grant = FakeSender(), permission(stream)
    await outbound.simulate(first["id"], sender, lambda: grant)
    second = await prepared(store, stream, service, outbound)
    assert (await outbound.simulate(second["id"], sender, lambda: grant))["outcome"] == "blocked"
    assert (
        len(sender.messages) == 1 and outbound.repository.get(second["id"])["status"] == "prepared"
    )


async def test_outbox_agent_and_conversation_scope(state):
    store, stream, _ = state
    service, outbound = pipeline(store, stream)
    item = await prepared(store, stream, service, outbound)
    other = store.create_agent("Other", "prompt")
    foreign = OutboxRepository(store, other.id)
    with pytest.raises(HistoryError):
        foreign.get(item["id"])
    assert foreign.listing()["items"] == []
    with pytest.raises(HistoryError):
        foreign.finish(item["id"], "accepted", service.clock.now())


async def test_default_cli_simulation_is_explicit_and_http_stays_read_only(observatory):
    client, store, chat, _, _ = observatory
    stream = chat.sessions.current.id
    service, outbound = pipeline(store, stream)
    chat.organization.proactive = service
    item = await prepared(store, stream, service, outbound)
    commands = MaintenanceCommands(chat.organization)
    result = await commands.proactive_command("simulate " + item["id"])
    assert result["mode"] == "simulation" and result["outcome"] == "accepted"
    before = store._connection.total_changes
    for kind in ["outbox", "attempts"]:
        response = await client.get("/api/proactive/" + kind)
        assert response.status_code == 200 and response.json()["items"]
        assert (await client.post("/api/proactive/" + kind)).status_code == 405
    assert store._connection.total_changes == before
    assert len(store.list_runs(stream)) == 1


async def test_daily_limit_and_concurrent_claim_are_host_enforced(state):
    store, stream, _ = state
    service, outbound = pipeline(store, stream)
    first = await prepared(store, stream, service, outbound)
    entered, release = asyncio.Event(), asyncio.Event()

    class Blocking(FakeSender):
        calls = 0

        async def send(self, *args):
            self.calls += 1
            entered.set()
            await release.wait()
            return await super().send(*args)

    sender, grant = Blocking(), permission(stream, cooldown_seconds=0, daily_limit=1)
    task = asyncio.create_task(outbound.simulate(first["id"], sender, lambda: grant))
    await entered.wait()
    assert (await outbound.simulate(first["id"], sender, lambda: grant))["outcome"] == "blocked"
    release.set()
    assert (await task)["outcome"] == "accepted" and sender.calls == 1
    second = await prepared(store, stream, service, outbound)
    assert (await outbound.simulate(second["id"], sender, lambda: grant))["outcome"] == "blocked"
    assert sender.calls == 1


async def test_input_received_while_grant_is_read_still_prevents_claim(state):
    store, stream, _ = state
    service, outbound = pipeline(store, stream)
    item = await prepared(store, stream, service, outbound)
    calls = 0
    grant = permission(stream)

    def current():
        nonlocal calls
        calls += 1
        if calls == 2:
            service.activity_epoch += 1
        return grant

    sender = FakeSender()
    assert (await outbound.simulate(item["id"], sender, current))["outcome"] == "stale"
    assert not sender.messages


def test_process_exit_after_effect_recovers_uncertain_without_replay(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    path, effect = tmp_path / "crash.db", tmp_path / "effect.txt"
    code = """
import asyncio, os, socket, sys
from pathlib import Path
from personal_agent.sessions import SessionManager
from personal_agent.storage.sqlite import SQLiteStore
from tests.test_outbound import pipeline, prepared, permission
from personal_agent.proactive.delivery import FakeSender
original = socket.socket.connect
def local_only(sock, address):
    if sock.family in {socket.AF_INET, socket.AF_INET6} and address[0] not in {'127.0.0.1', '::1'}:
        raise AssertionError('offline only')
    return original(sock, address)
socket.socket.connect = local_only
with SQLiteStore(Path(sys.argv[1])) as store:
    store.initialize()
    stream = SessionManager(store).current.id
    service, outbound = pipeline(store, stream)
    class ExitSender(FakeSender):
        async def send(self, *args):
            await super().send(*args)
            Path(sys.argv[2]).write_text('effect', encoding='utf-8')
            os._exit(23)
    async def run():
        item = await prepared(store, stream, service, outbound)
        await outbound.simulate(item['id'], ExitSender(), lambda: permission(stream))
    asyncio.run(run())
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", code, str(path), str(effect)],
        env=environment,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 23 and effect.read_text(encoding="utf-8") == "effect"
    with SQLiteStore(path) as store:
        store.initialize()
        repository = OutboxRepository(store, "heybuddy")
        assert repository.listing()["items"][0]["status"] == "sending"
        from datetime import UTC, datetime

        repository.recover(datetime.now(UTC))
        assert repository.listing()["items"][0]["status"] == "uncertain"
        assert repository.listing("attempts")["items"][0]["status"] == "uncertain"
        assert len(repository.listing("attempts")["items"]) == 1


def test_schema_16_to_17_atomic_migration_and_rollback(tmp_path, monkeypatch):
    path = tmp_path / "outbox.db"
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 16)
        with SQLiteStore(path) as store:
            store.initialize()
            sessions = SessionManager(store)
            seed(store, sessions.current.id, "历史保留")
    with monkeypatch.context() as patch:
        patch.setitem(storage.MIGRATIONS, 17, (*storage.MIGRATIONS[17], "INVALID SQL"))
        with SQLiteStore(path) as store:
            with pytest.raises(HistoryError):
                store.initialize()
            assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 16
            assert (
                store._connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='proactive_outbox'"
                ).fetchone()
                is None
            )
    with SQLiteStore(path) as store:
        store.initialize()
        assert (
            store._connection.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
        )
        assert len(store.list_runs(sessions.current.id)) == 1
