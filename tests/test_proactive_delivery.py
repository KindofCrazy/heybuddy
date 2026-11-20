"""Real-channel contracts exercised with isolated journals and fake local sockets."""

import asyncio
from datetime import timedelta, timezone

import pytest

from personal_agent.channels.napcat.events import parse_message
from personal_agent.channels.napcat.proactive import NapCatProactiveSender
from personal_agent.channels.napcat.transport import NapCatTransport
from personal_agent.core.execution import CURRENT_SCOPE, CURRENT_THREAD, RunScope
from personal_agent.core.history import HistoryError
from personal_agent.proactive.delivery import DeliveryError, DeliveryGrant
from personal_agent.scheduler.chat import source_scope
from personal_agent.sessions import SessionManager
from personal_agent.storage import sqlite as storage
from personal_agent.storage.outbox import OutboundContext, OutboxRepository
from personal_agent.storage.qq import QQInbox
from personal_agent.storage.routing import RoutingRepository
from personal_agent.storage.sqlite import SQLiteStore
from tests.test_episodes import seed, state
from tests.test_napcat import Socket, account, event, start_transport
from tests.test_outbound import TEXT, pipeline, prepared
from tests.test_qq_transport import config
from tests.test_reminder_delivery import Sender

__all__ = ["state"]


def bind(store, stream, outbound, sender=None):
    selected = account()
    inbox = QQInbox(store, "heybuddy", selected.napcat_scope, selected.napcat_peer_id)
    grant = DeliveryGrant(
        enabled=True,
        channel="napcat",
        conversation_id=stream,
        recipient=selected.napcat_peer_id,
        channel_scope=selected.napcat_scope,
        source_scope=source_scope(inbox.scope),
    )
    sender = sender or Sender()
    outbound.bind_delivery(sender, lambda: grant)
    return sender, grant, inbox


def link(inbox, item):
    proposal = inbox.store._connection.execute(
        "SELECT json_extract(sources,'$[0].run_id') FROM proactive_proposals WHERE id=?",
        (item["proposal_id"],),
    ).fetchone()
    message_id = (
        inbox.store._connection.execute("SELECT COUNT(*) FROM qq_receipts").fetchone()[0] + 1
    )
    message = parse_message(event(message_id), account(), config())
    inbox.receive(message, item["conversation_id"], 16)
    row = inbox.claim(item["conversation_id"])
    inbox.link_run(row["sequence"], proposal[0])
    inbox.mark(row["sequence"], "replied")


async def test_live_checkin_is_claimed_once_and_continues_only_its_source_and_thread(state):
    store, stream, _ = state
    service, outbound = pipeline(store, stream)
    sender, grant, inbox = bind(store, stream, outbound)
    item = await prepared(store, stream, service, outbound)
    link(inbox, item)
    assert service.status()["mode"] == "napcat"
    assert (await service.run())["outcome"] == "accepted"
    current = outbound.repository.get(item["id"])
    assert current["mode"] == "napcat" and current["receipt_id"] == "42"
    assert sender.messages == [(grant.recipient, TEXT, "proactive:" + item["id"])]
    assert (await service.run())["outcome"] == "no_proactive_work"
    assert len(store.list_runs(stream)) == 1
    assert len(service.model.provider.requests) == 3
    notices = OutboundContext(outbound.repository, stream, source_scope=grant.source_scope)
    packet = notices.context()
    assert packet["simulation"] is False and packet["user_evidence"] is False
    assert packet["messages"][0]["text"] == TEXT
    assert OutboundContext(outbound.repository, stream, source_scope="cli").context() is None
    scope = CURRENT_SCOPE.set(RunScope("heybuddy", stream, grant.source_scope))
    repository = RoutingRepository(store, "heybuddy", stream, grant.source_scope)
    a, b = repository.create("A"), repository.create("B")
    run_id = store.list_runs(stream)[0].id
    with store._transaction() as db:
        db.execute(
            "INSERT INTO run_threads(run_id,conversation_id,source,thread_id,reason) "
            "VALUES(?,?,?,?,'test')",
            (run_id, stream, grant.source_scope, a.id),
        )
    thread = CURRENT_THREAD.set(b.id)
    try:
        assert notices.context() is None
        CURRENT_THREAD.set(a.id)
        assert notices.context()["messages"][0]["mode"] == "napcat"
        with store._transaction() as db:
            db.execute("UPDATE proactive_proposals SET sources='[]'")
        # A later proposal update cannot move the already-sent notice to another Thread.
        assert notices.context()["messages"][0]["mode"] == "napcat"
    finally:
        CURRENT_THREAD.reset(thread)
        CURRENT_SCOPE.reset(scope)
    assert TEXT not in [m.content.text for m in store.load_messages(stream)]


@pytest.mark.parametrize("change", ["cli", "peer", "channel", "conversation"])
async def test_foreign_sources_cannot_be_delivered_to_bound_peer(state, change):
    store, stream, _ = state
    service, outbound = pipeline(store, stream)
    sender, grant, inbox = bind(store, stream, outbound)
    item = await prepared(store, stream, service, outbound)
    if change != "cli":
        link(inbox, item)
        other = store.create_conversation("heybuddy") if change == "conversation" else None
        with store._transaction() as db:
            if change == "peer":
                db.execute("UPDATE qq_receipts SET openid='1111111'")
            elif change == "channel":
                db.execute("UPDATE qq_receipts SET app_id='official'")
            else:
                db.execute("UPDATE qq_receipts SET conversation_id=?", (other.id,))
    assert (await service.run())["outcome"] == "blocked"
    assert not sender.messages and not outbound.repository.listing("attempts")["items"]
    assert outbound.repository.get(item["id"])["status"] == "cancelled"


@pytest.mark.parametrize("stage", ["draft", "decision"])
async def test_reconnect_never_sends_old_draft_or_old_notify_decision(state, stage):
    store, stream, _ = state
    service, outbound = pipeline(store, stream)
    seed(store, stream)
    await service.run()
    await service.run()
    if stage == "draft":
        await service.run()
    service.clock.time += timedelta(seconds=1)
    sender, _, _ = bind(store, stream, outbound)
    if stage == "decision":
        assert (await service.run())["stage"] == "composer"
    assert not outbound.ready()
    assert (await service.run())["outcome"] == "no_proactive_work"
    assert not sender.messages


async def test_live_rate_limit_withdraws_second_draft_without_delayed_replay(state):
    store, stream, _ = state
    service, outbound = pipeline(store, stream)
    sender, _, inbox = bind(store, stream, outbound)
    first = await prepared(store, stream, service, outbound)
    link(inbox, first)
    assert (await service.run())["outcome"] == "accepted"
    second = await prepared(store, stream, service, outbound)
    link(inbox, second)
    assert (await service.run())["outcome"] == "blocked"
    assert outbound.repository.get(second["id"])["status"] == "cancelled"
    assert len(sender.messages) == 1
    assert outbound.repository.listing("attempts")["items"][0]["mode"] == "napcat"


@pytest.mark.parametrize("change", ["quiet", "disabled", "covered"])
async def test_quiet_hours_disabled_policy_and_covered_plan_never_send(state, change):
    store, stream, _ = state
    service, outbound = pipeline(store, stream)
    sender, _, inbox = bind(store, stream, outbound)
    item = await prepared(store, stream, service, outbound)
    link(inbox, item)
    if change == "quiet":
        hour = service.clock.now().astimezone(timezone(timedelta(hours=8))).hour
        service.policy.quiet_start_hour = hour
        service.policy.quiet_end_hour = (hour + 1) % 24
    elif change == "disabled":
        service.policy.enabled = False
    else:
        with store._transaction() as db:
            db.execute(
                "UPDATE proactive_proposals SET followup_state='covered',revision=revision+1"
            )
    await service.run()
    assert not sender.messages and not outbound.repository.listing("attempts")["items"]


@pytest.mark.parametrize("failure", [DeliveryError(), DeliveryError(uncertain=False)])
async def test_live_failed_or_uncertain_results_are_terminal(state, failure):
    store, stream, _ = state
    service, outbound = pipeline(store, stream)
    sender, _, inbox = bind(store, stream, outbound, Sender(failure))
    item = await prepared(store, stream, service, outbound)
    link(inbox, item)
    expected = "uncertain" if failure.uncertain else "failed"
    assert (await service.run())["outcome"] == expected
    outbound.repository.recover(service.clock.now())
    assert (await service.run())["outcome"] == "no_proactive_work"
    assert len(sender.messages) == 1
    assert OutboundContext(outbound.repository, stream).context() is None


@pytest.mark.parametrize("change", ["input", "disconnect", "switch", "disable"])
async def test_final_check_after_send_delay_prevents_external_send(state, change):
    store, stream, _ = state
    service, outbound = pipeline(store, stream)
    transport = NapCatTransport(account(), config())
    socket = Socket()
    task, _ = await start_transport(transport, socket)
    sender = NapCatProactiveSender(transport, outbound.delivery_allowed)
    _, _, inbox = bind(store, stream, outbound, sender)
    item = await prepared(store, stream, service, outbound)
    link(inbox, item)
    sender._next_send = asyncio.get_running_loop().time() + 0.1
    sending = asyncio.create_task(service.run())
    try:
        for _ in range(100):
            if outbound.repository.get(item["id"])["status"] == "sending":
                break
            await asyncio.sleep(0)
        assert outbound.repository.get(item["id"])["status"] == "sending"
        if change == "input":
            service.activity_epoch += 1
        elif change == "disconnect":
            transport.state = "reconnecting"
        elif change == "switch":
            outbound.conversation = lambda: "other"
        else:
            transport.account.napcat_proactive_enabled = False
        assert (await sending)["outcome"] == "failed"
        assert len(socket.sent) == 1  # Login check only.
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_real_transport_preserves_literal_text_and_true_receipt():
    transport = NapCatTransport(account(), config())
    socket = Socket()
    task, _ = await start_transport(transport, socket)
    try:
        sender = NapCatProactiveSender(transport, lambda item: item == "example")
        receipt = await sender.send("7654321", "最近怎么样 [CQ:at,qq=all]", "proactive:example")
        assert receipt.receipt_id == "88"
        assert socket.sent[-1]["params"] == {
            "user_id": 7654321,
            "message": [{"type": "text", "data": {"text": "最近怎么样 [CQ:at,qq=all]"}}],
        }
        for recipient, key in [("1111111", "proactive:example"), ("7654321", "scheduled:x")]:
            with pytest.raises(DeliveryError):
                await sender.send(recipient, "拒绝", key)
        assert len(socket.sent) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_migration_24_to_25_is_atomic_and_preserves_legacy_rows(tmp_path, monkeypatch):
    path = tmp_path / "upgrade.db"
    original = storage.MIGRATIONS[25]
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 24)
        with SQLiteStore(path) as store:
            store.initialize()
            stream = SessionManager(store).current.id
            service, outbound = pipeline(store, stream)
            seed(store, stream)
            await service.run()
            await service.run()
            decision = outbound.repository.next_decision()
            proposal = service.repository.candidates()[0]
            at = service.clock.now().isoformat()
            with store._transaction() as db:
                db.execute(
                    "INSERT INTO proactive_outbox "
                    "(id,agent_id,decision_id,proposal_id,proposal_revision,conversation_id,"
                    "activity,receipt_epoch,status,mode,text,persona_fingerprint,"
                    "created_at,expires_at,updated_at) "
                    "VALUES ('legacy','heybuddy',?,?,?,?,'[]',0,'prepared','preview',"
                    "?,'old',?,?,?)",
                    (
                        decision["id"],
                        proposal["id"],
                        proposal["revision"],
                        stream,
                        TEXT,
                        at,
                        proposal["expires_at"],
                        at,
                    ),
                )
    with SQLiteStore(path) as store:
        with monkeypatch.context() as patch:
            patch.setitem(storage.MIGRATIONS, 25, (*original, "INVALID SQL"))
            with pytest.raises(HistoryError):
                store.initialize()
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 24
        columns = store._connection.execute("PRAGMA table_info(proactive_outbox)").fetchall()
        assert "delivery_channel" not in {row[1] for row in columns}
        store.initialize()
        assert store._connection.execute("PRAGMA user_version").fetchone()[0] == 25
        assert store._connection.execute("PRAGMA foreign_key_check").fetchall() == []
        current = OutboxRepository(store, "heybuddy").get("legacy")
        assert current["text"] == TEXT and current["mode"] == "preview"
