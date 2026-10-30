"""Deferred offline cases for explicit chat scheduling; never reaches real channels."""

from datetime import UTC, datetime, timedelta

import pytest

from personal_agent.channels.qq.adapter import QQAdapter
from personal_agent.scheduler.chat import reply, source_scope
from personal_agent.scheduler.models import ReminderError, SchedulerPolicy
from personal_agent.scheduler.runtime import SchedulerRuntime
from personal_agent.storage import sqlite as storage
from personal_agent.storage.reminders import ReminderRepository
from personal_agent.storage.sqlite import SQLiteStore
from tests.test_qq_adapter import Sender, event, rows, settled, state
from tests.test_qq_transport import config

__all__ = ["state"]


def attach(state):
    db, sessions, _, service, _ = state
    service.scheduler = SchedulerRuntime(
        ReminderRepository(db, sessions.agent.id), SchedulerPolicy()
    )
    return service.scheduler


async def test_chat_creates_once_without_agent_or_turn_judge_and_cancels(state):
    db, sessions, provider, service, inbox = state
    scheduler = attach(state)
    sender = Sender()
    adapter = QQAdapter(
        config(qq_message_mode="hybrid", qq_humanize_enabled=False), service, inbox, sender
    )
    adapter.start()
    try:
        await adapter.accept(event("create", text="/remind in 10m 喝水"))
        await settled(adapter)
        await adapter.accept(event("create", text="/remind in 10m 喝水"))
        items = scheduler.repository.listing()["items"]
        assert len(items) == 1 and items[0]["status"] == "scheduled"
        assert items[0]["source_scope"] == source_scope(inbox.scope)
        assert "尚无到期主动发送渠道" in "".join(message[3] for message in sender.messages)
        await adapter.accept(event("cancel", text="/remind cancel " + items[0]["id"]))
        await settled(adapter)
        assert scheduler.repository.get(items[0]["id"])["status"] == "cancelled"
        assert not provider.requests and not db.list_runs(sessions.current.id)
        assert all(row["run_id"] is None for row in rows(db))
    finally:
        await adapter.close()
        await scheduler.close()


@pytest.mark.parametrize(
    "text",
    ["我明天准备做一个demo", "明天九点提醒我喝水", "讨论一下 /remind in 10m 喝水"],
)
async def test_ordinary_chat_does_not_create_schedule(state, text):
    _, _, provider, service, inbox = state
    scheduler = attach(state)
    adapter = QQAdapter(config(), service, inbox, Sender())
    adapter.start()
    try:
        await adapter.accept(event(text=text))
        await settled(adapter)
        assert len(provider.requests) == 1
        assert scheduler.repository.listing()["items"] == []
    finally:
        await adapter.close()


async def test_channel_scope_and_receipt_anchor_are_durable(state):
    _, sessions, provider, _, inbox = state
    scheduler = attach(state)
    received = datetime.now(UTC)
    scope = source_scope(inbox.scope)
    args = dict(scope=scope, message_id="same", received_at=received, has_attachments=False)
    first = await reply(scheduler, sessions.current.id, "/remind in 10m 喝水", **args)
    assert await reply(scheduler, sessions.current.id, "/remind in 10m 喝水", **args) == first
    item = scheduler.repository.listing()["items"][0]
    assert datetime.fromisoformat(item["due_at"]) == received + timedelta(minutes=10)
    foreign = source_scope((inbox.agent_id, "another-channel", inbox.openid))
    args["scope"] = foreign
    assert (
        await reply(scheduler, sessions.current.id, "/remind list", **args)
        == "当前聊天还没有提醒记录。"
    )
    with pytest.raises(ReminderError, match="not_found"):
        await reply(scheduler, sessions.current.id, "/remind cancel " + item["id"], **args)
    assert scheduler.repository.get(item["id"])["status"] == "scheduled"
    assert not provider.requests


@pytest.mark.parametrize("cursor", ["0", "-1", str(2**63), "invalid"])
async def test_chat_rejects_invalid_page_without_sql_overflow(state, cursor):
    _, sessions, _, _, inbox = state
    scheduler = attach(state)
    with pytest.raises(ReminderError, match="invalid_arguments"):
        await reply(
            scheduler,
            sessions.current.id,
            "/remind list " + cursor,
            scope=source_scope(inbox.scope),
            message_id="list",
            received_at=datetime.now(UTC),
            has_attachments=False,
        )


async def test_chat_rejects_simulation_management_and_attachments(state):
    db, sessions, provider, service, inbox = state
    scheduler = attach(state)
    sender = Sender()
    adapter = QQAdapter(config(qq_humanize_enabled=False), service, inbox, sender)
    adapter.start()
    try:
        for index, text in enumerate(
            ["/remind simulate any", "/remind status", "/memory edit anything"]
        ):
            await adapter.accept(event(str(index), text=text))
        await adapter.accept(event("stranger", text="/remind in 10m leak", openid="other"))
        await settled(adapter)
        args = dict(
            scope=source_scope(inbox.scope),
            message_id="image",
            received_at=datetime.now(UTC),
            has_attachments=True,
        )
        assert "纯文本" in await reply(
            scheduler, sessions.current.id, "/remind in 10m 喝水", **args
        )
        assert scheduler.repository.listing()["items"] == []
        assert not provider.requests and not db.list_runs(sessions.current.id)
    finally:
        await adapter.close()


async def test_passive_ack_failure_does_not_undo_or_replay_schedule(state):
    _, _, _, service, inbox = state
    scheduler = attach(state)
    from personal_agent.channels.qq.config import QQError

    sender = Sender(error=QQError("synthetic", uncertain=True))
    adapter = QQAdapter(config(qq_humanize_enabled=False), service, inbox, sender)
    adapter.start()
    try:
        await adapter.accept(event("create", text="/remind in 10m 喝水"))
        await settled(adapter)
        assert len(scheduler.repository.listing()["items"]) == 1
        await adapter.accept(event("create", text="/remind in 10m 喝水"))
        assert len(scheduler.repository.listing()["items"]) == 1
    finally:
        await adapter.close()


def test_schema_18_to_19_failure_rolls_back_scope_column(tmp_path, monkeypatch):
    path = tmp_path / "scope.db"
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 18)
        with SQLiteStore(path) as db:
            db.initialize()
    with monkeypatch.context() as patch:
        patch.setitem(storage.MIGRATIONS, 19, (*storage.MIGRATIONS[19], "INVALID SQL"))
        with SQLiteStore(path) as db:
            from personal_agent.core.history import HistoryError

            with pytest.raises(HistoryError):
                db.initialize()
            assert db._connection.execute("PRAGMA user_version").fetchone()[0] == 18
            columns = db._connection.execute("PRAGMA table_info(scheduled_reminders)").fetchall()
            assert "source_scope" not in {row[1] for row in columns}
