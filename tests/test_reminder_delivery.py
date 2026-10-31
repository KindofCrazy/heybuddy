"""Offline tests of real-mode contracts; all wire traffic goes to a fake local socket."""

import asyncio
from datetime import timedelta

import pytest

from personal_agent.channels.napcat.reminders import NapCatReminderSender
from personal_agent.channels.napcat.transport import NapCatTransport
from personal_agent.core.history import HistoryError
from personal_agent.proactive.delivery import DeliveryError, SendReceipt
from personal_agent.scheduler.chat import describe, source_scope
from personal_agent.scheduler.models import ReminderGrant, ReminderRequest
from personal_agent.scheduler.runtime import SchedulerRuntime
from personal_agent.storage import sqlite as storage
from personal_agent.storage.sqlite import SQLiteStore
from tests.test_napcat import Socket, account, start_transport
from tests.test_qq_transport import config
from tests.test_scheduler import reminders

__all__ = ["reminders"]


class Sender:
    mode = "napcat"

    def __init__(self, failure=None):
        self.messages = []
        self.failure = failure

    async def send(self, recipient, text, key):
        self.messages.append((recipient, text, key))
        if self.failure:
            raise self.failure
        return SendReceipt(receipt_id="42")


def bind(reminders, sender=None):
    _, sessions, runtime, clock = reminders
    scope = source_scope((sessions.agent.id, "napcat:12345:7654321", "7654321"))
    grant = ReminderGrant(
        enabled=True,
        channel="napcat",
        recipient="7654321",
        conversation_id=sessions.current.id,
        source_scope=scope,
    )
    runtime.tick()
    sender = sender or Sender()
    runtime.bind_delivery(sender, lambda: grant)
    item = runtime.create(
        ReminderRequest(
            request_id="live",
            conversation_id=sessions.current.id,
            source_scope=scope,
            due_at=clock.now() + timedelta(seconds=10),
            text="喝水",
        )
    )["reminder"]
    return sender, grant, item


async def test_due_live_reminder_records_true_mode_once_without_proactive_or_journal(reminders):
    store, sessions, runtime, clock = reminders
    sender, grant, item = bind(reminders)
    assert runtime.delivery_for(sessions.current.id, grant.source_scope)
    clock.time += timedelta(seconds=10)
    assert await runtime.deliver_due() == 1
    current = runtime.repository.get(item["id"])
    assert current["status"] == "accepted" and current["mode"] == "napcat"
    assert current["receipt_id"] == "42" and "渠道已接受" in describe(current)
    assert await runtime.deliver_due() == 0 and len(sender.messages) == 1
    assert not store.list_runs(sessions.current.id)
    assert store._connection.execute("SELECT COUNT(*) FROM proactive_proposals").fetchone()[0] == 0


async def test_scope_local_reminders_and_revoked_grant_do_not_send(reminders):
    _, sessions, runtime, clock = reminders
    sender, grant, item = bind(reminders)
    runtime.create(
        ReminderRequest(
            request_id="local",
            conversation_id=sessions.current.id,
            due_at=clock.now() + timedelta(seconds=10),
            text="本地记录不向 QQ 发送",
        )
    )
    runtime.bind_delivery(sender, lambda: grant.model_copy(update={"enabled": False}))
    clock.time += timedelta(seconds=10)
    assert await runtime.deliver_due() == 0 and not sender.messages
    runtime.bind_delivery(sender, lambda: grant.model_copy(update={"source_scope": "chat:foreign"}))
    assert await runtime.deliver_due() == 0 and not sender.messages
    assert runtime.repository.get(item["id"])["status"] == "scheduled"


async def test_reconnect_or_restart_never_catches_up_already_due_records(reminders):
    _, _, runtime, clock = reminders
    sender, grant, item = bind(reminders)
    clock.time += timedelta(seconds=20)
    runtime.unbind_delivery()
    runtime.tick()
    runtime.bind_delivery(sender, lambda: grant)
    assert await runtime.deliver_due() == 0
    assert runtime.repository.get(item["id"])["status"] == "blocked"
    restarted = SchedulerRuntime(runtime.repository, runtime.policy, clock=clock)
    restarted.tick()
    restarted.bind_delivery(sender, lambda: grant)
    assert await restarted.deliver_due() == 0 and not sender.messages


@pytest.mark.parametrize("uncertain", [True, False])
async def test_live_failure_is_terminal_and_never_retried(reminders, uncertain):
    _, _, runtime, clock = reminders
    sender, _, item = bind(reminders, Sender(DeliveryError(uncertain=uncertain)))
    clock.time += timedelta(seconds=10)
    assert await runtime.deliver_due() == 1
    assert runtime.repository.get(item["id"])["status"] == ("uncertain" if uncertain else "failed")
    assert await runtime.deliver_due() == 0 and len(sender.messages) == 1


async def test_cancel_during_last_grant_check_prevents_claim(reminders):
    _, _, runtime, clock = reminders
    sender, grant, item = bind(reminders)
    calls = 0

    def current():
        nonlocal calls
        calls += 1
        if calls == 3:
            runtime.cancel(item["id"])
        return grant

    runtime.bind_delivery(sender, current)
    clock.time += timedelta(seconds=10)
    assert await runtime.deliver_due() == 0 and not sender.messages
    assert runtime.repository.get(item["id"])["status"] == "cancelled"


async def test_napcat_reminder_uses_bound_private_text_and_true_receipt():
    transport = NapCatTransport(account(), config())
    socket = Socket()
    task, _ = await start_transport(transport, socket)
    try:
        sender = NapCatReminderSender(transport)
        receipt = await sender.send("7654321", "投递测试 [CQ:at,qq=all]", "scheduled:example")
        assert receipt.receipt_id
        assert socket.sent[1]["action"] == "send_private_msg"
        assert socket.sent[1]["params"] == {
            "user_id": 7654321,
            "message": [{"type": "text", "data": {"text": "提醒：投递测试 [CQ:at,qq=all]"}}],
        }
        with pytest.raises(DeliveryError):
            await sender.send("1111111", "不得发送", "scheduled:wrong-peer")
        assert len(socket.sent) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_schema_20_to_21_failure_rolls_back_delivery_column(tmp_path, monkeypatch):
    path = tmp_path / "delivery.db"
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 20)
        with SQLiteStore(path) as db:
            db.initialize()
    with monkeypatch.context() as patch:
        patch.setitem(storage.MIGRATIONS, 21, (*storage.MIGRATIONS[21], "INVALID SQL"))
        with SQLiteStore(path) as db:
            with pytest.raises(HistoryError):
                db.initialize()
            assert db._connection.execute("PRAGMA user_version").fetchone()[0] == 20
            columns = db._connection.execute("PRAGMA table_info(scheduled_reminders)").fetchall()
            assert "delivery_channel" not in {r[1] for r in columns}


def test_old_simulation_receipt_retains_its_mode_after_migration(tmp_path, monkeypatch):
    from personal_agent.sessions import SessionManager

    path = tmp_path / "legacy.db"
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 20)
        with SQLiteStore(path) as db:
            db.initialize()
            sessions = SessionManager(db)
            with db._transaction() as connection:
                value = "2026-10-31T00:00:00+00:00"
                connection.execute(
                    "INSERT INTO scheduled_reminders "
                    "(id,agent_id,conversation_id,request_id,text,due_at,expires_at,status,"
                    "mode,created_at,updated_at) VALUES "
                    "(?,?,?,?,?,?,?,'accepted','simulation',?,?)",
                    (
                        "old",
                        sessions.agent.id,
                        sessions.current.id,
                        "old",
                        "合成提醒",
                        value,
                        value,
                        value,
                        value,
                    ),
                )
    with SQLiteStore(path) as db:
        db.initialize()
        from personal_agent.storage.reminders import ReminderRepository

        item = ReminderRepository(db, sessions.agent.id).get("old")
        assert item["mode"] == "simulation" and item["delivery_channel"] == ""
