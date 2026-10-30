import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from personal_agent.channels.qq.adapter import QQAdapter
from personal_agent.channels.qq.config import QQError, QQSettings
from personal_agent.channels.qq.events import QQAttachment, parse_text
from personal_agent.channels.qq.turns import plan_turn
from personal_agent.config import load_qq_settings, load_settings
from tests.test_qq_adapter import Sender, event, rows, settled, state
from tests.test_qq_transport import config

__all__ = ["state"]


def receive(inbox, conversation_id, index, offset, text=None, attachments=()):
    message = parse_text(event(str(index), text if text is not None else str(index)), config())
    message = replace(
        message,
        received_at=datetime(2026, 10, 30, tzinfo=UTC) + timedelta(seconds=offset),
        attachments=attachments,
    )
    assert inbox.receive(message, conversation_id, 100)
    return message.received_at.timestamp()


def test_deadlines_reset_and_claim_is_atomic_and_deduplicated(state):
    db, sessions, _, _, inbox = state
    settings = QQSettings()
    base = receive(inbox, sessions.current.id, 1, 0)
    assert inbox.claim_turn(sessions.current.id, settings, now=base + 2)[1] == 0.5
    receive(inbox, sessions.current.id, 2, 2)
    assert inbox.claim_turn(sessions.current.id, settings, now=base + 3)[1] == 1.5
    receive(inbox, sessions.current.id, 3, 4)
    assert [r["status"] for r in rows(db)] == ["received"] * 3
    turn, delay = inbox.claim_turn(sessions.current.id, settings, now=base + 6.5)
    assert delay == 0 and turn["content"] == "1\n2\n3"
    assert turn["message_id"] == "3" and turn["sequences"] == (1, 2, 3)
    assert [r["status"] for r in rows(db)] == ["processing"] * 3
    assert inbox.claim_turn(sessions.current.id, settings, now=base + 10) == (None, 0)
    assert not inbox.receive(parse_text(event("2"), settings), sessions.current.id, 16)
    duplicate = event("different-id")
    duplicate["id"] = "event-1"
    assert not inbox.receive(parse_text(duplicate, settings), sessions.current.id, 16)


def test_maximum_wait_and_exact_boundary_split_backlog(state):
    db, sessions, _, _, inbox = state
    for index, offset in enumerate((0, 2, 4, 6, 7.9, 8), 1):
        base = receive(inbox, sessions.current.id, index, offset) - offset
    turn, delay = inbox.claim_turn(sessions.current.id, QQSettings(), now=base + 8)
    assert delay == 0 and turn["content"] == "1\n2\n3\n4\n5"
    assert rows(db)[-1]["status"] == "received"
    # An idle boundary is also honored when the consumer was busy or offline.
    inbox.mark(turn["sequences"], "replied")
    receive(inbox, sessions.current.id, 7, 10.5)
    turn, _ = inbox.claim_turn(sessions.current.id, QQSettings(), now=base + 20)
    assert turn["content"] == "6"


@pytest.mark.parametrize("boundary", ["command", "bytes", "images", "count"])
def test_bounds_split_without_losing_receipts_or_attachments(state, boundary):
    db, sessions, _, _, inbox = state
    settings = QQSettings(qq_input_max_bytes=128, qq_image_max_count=1)
    count = 17 if boundary == "count" else 2
    for index in range(count):
        receive(
            inbox,
            sessions.current.id,
            index,
            index * 0.01,
            text="/new"
            if boundary == "command" and index
            else ("a" * 100 if boundary == "bytes" else str(index)),
            attachments=(QQAttachment("https://qpic.cn/test"),) if boundary == "images" else (),
        )
    original = rows(db)
    turn, _ = inbox.claim_turn(sessions.current.id, settings)
    expected = 16 if boundary == "count" else 1
    assert len(turn["receipts"]) == expected
    assert [r["status"] for r in rows(db)] == ["processing"] * expected + ["received"]
    inbox.release(turn["receipts"])
    assert rows(db) == original


def test_image_positions_and_order_are_preserved(state):
    db, sessions, _, _, inbox = state
    receive(inbox, sessions.current.id, 1, 0, "先看", (QQAttachment("one"),))
    receive(inbox, sessions.current.id, 2, 1, "再看", (QQAttachment("two"),))
    turn = plan_turn(rows(db), QQSettings()).input_row()
    assert turn["content"] == "先看\n[本条消息附带图片：1]\n再看\n[本条消息附带图片：2]"
    assert [a["url"] for a in json.loads(turn["attachments"])] == ["one", "two"]


@pytest.mark.parametrize(
    "error,status", [(None, "replied"), (TimeoutError(), "uncertain"), (QQError("send"), "failed")]
)
async def test_batch_runs_and_replies_once_with_shared_run_and_status(
    state, error, status, monkeypatch
):
    db, sessions, provider, service, inbox = state
    completed = []
    monkeypatch.setattr(
        type(service), "after_reply", lambda self, result=None: completed.append(result)
    )
    sender = Sender(error)
    adapter = QQAdapter(config(), service, inbox, sender)
    # All accept calls persist without yielding, including duplicates/unauthorized/empty input.
    for index in (1, 2, 2, 3):
        await adapter.accept(event(str(index), str(index)))
    await adapter.accept(event("unauthorized", openid="stranger"))
    await adapter.accept(event("empty", text=""))
    adapter.start()
    try:
        await settled(adapter)
        assert len(provider.requests) == len(sender.messages) == 1
        assert sender.messages[0][1:3] == ("3", 1)
        assert db.load_messages(sessions.current.id)[0].content.text == "1\n2\n3"
        run_id = db.list_runs(sessions.current.id)[0].id
        assert {r["run_id"] for r in rows(db)} == {run_id}
        assert {r["status"] for r in rows(db)} == {status}
        assert len(completed) == 1 and completed[0].status == "completed"
    finally:
        await adapter.close()


async def test_shutdown_during_wait_and_restart_preserve_input(state):
    db, _, provider, service, inbox = state
    adapter = QQAdapter(
        QQSettings(qq_app_id="12345", qq_allowed_openids=("test-openid",)), service, inbox, Sender()
    )
    await adapter.accept(event("1"))
    await adapter.accept(event("2"))
    adapter.start()
    await asyncio.sleep(0)  # Consumer reaches its wait, with no real-time delay.
    await adapter.close()
    assert not provider.requests and {r["status"] for r in rows(db)} == {"received"}
    restart = QQAdapter(config(), service, inbox, Sender())
    restart.start()
    try:
        await settled(restart)
        assert len(provider.requests) == 1
    finally:
        await restart.close()


async def test_shutdown_before_run_restores_claimed_batch(state):
    db, _, provider, service, inbox = state
    entered = asyncio.Event()

    class Images:
        async def prepare(self, attachments):
            entered.set()
            await asyncio.Event().wait()

    adapter = QQAdapter(config(), service, inbox, Sender(), Images())
    await adapter.accept(
        event("1", attachments=[{"content_type": "image", "url": "https://qpic.cn/1"}])
    )
    await adapter.accept(event("2"))
    adapter.start()
    await asyncio.wait_for(entered.wait(), 1)
    await adapter.close()
    assert not provider.requests
    assert {r["status"] for r in rows(db)} == {"received"}
    assert json.loads(rows(db)[0]["attachments"])[0]["url"] == "https://qpic.cn/1"


async def test_cancel_started_batch_links_run_and_never_recovers_it(state):
    db, sessions, _, service, inbox = state
    entered = asyncio.Event()

    class Provider:
        async def generate(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    service.agent.provider = Provider()
    adapter = QQAdapter(config(), service, inbox, Sender())
    await adapter.accept(event("1"))
    await adapter.accept(event("2"))
    adapter.start()
    await asyncio.wait_for(entered.wait(), 1)
    await adapter.accept(event("3"))
    await adapter.close()
    run = db.list_runs(sessions.current.id)[0]
    assert run.status == "cancelled"
    assert [r["run_id"] for r in rows(db)] == [run.id, run.id, None]
    inbox.recover()
    assert [r["status"] for r in rows(db)] == ["uncertain", "uncertain", "received"]


async def test_arrival_during_send_belongs_to_next_turn(state):
    db, _, provider, service, inbox = state
    from personal_agent.core.models import ModelResponse

    provider.responses = [ModelResponse(text="第一次"), ModelResponse(text="第二次")]
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingSender(Sender):
        async def send_text(self, *args):
            await super().send_text(*args)
            if len(self.messages) == 1:
                entered.set()
                await release.wait()

    sender = BlockingSender()
    adapter = QQAdapter(config(qq_send_interval_seconds=0.1), service, inbox, sender)
    adapter.start()
    try:
        await adapter.accept(event("1"))
        await asyncio.wait_for(entered.wait(), 1)
        await adapter.accept(event("2"))
        await adapter.accept(event("3"))
        assert [r["status"] for r in rows(db)] == ["sending", "received", "received"]
        release.set()
        await settled(adapter)
        assert len(provider.requests) == 2
        assert [m[1] for m in sender.messages] == ["1", "3"]
    finally:
        await adapter.close()


@pytest.mark.parametrize(
    "values",
    [
        {"qq_message_idle_seconds": 0},
        {"qq_message_idle_seconds": -1},
        {"qq_message_idle_seconds": float("nan")},
        {"qq_message_max_wait_seconds": float("inf")},
        {"qq_message_max_wait_seconds": 2},
        {"qq_message_idle_seconds": 31},
        {"qq_message_max_wait_seconds": 61},
    ],
)
def test_invalid_config(values):
    with pytest.raises(ValidationError):
        QQSettings(**values)


def test_defaults_and_environment_aliases(monkeypatch, tmp_path):
    assert QQSettings().qq_message_idle_seconds == 2.5
    assert QQSettings().qq_message_max_wait_seconds == 8
    for key, value in {
        "QQ_MESSAGE_IDLE_SECONDS": "1",
        "QQ_MESSAGE_MAX_WAIT_SECONDS": "4",
        "OPENAI_API_KEY": "synthetic",
        "OPENAI_MODEL": "synthetic",
        "AGENT_CONTEXT_WINDOW": "64000",
    }.items():
        monkeypatch.setenv(key, value)
    assert load_settings(tmp_path / "missing").qq_message_idle_seconds == 1
    assert load_qq_settings(tmp_path / "missing").qq_message_max_wait_seconds == 4
    monkeypatch.setenv("AGENT_QQ_MESSAGE_IDLE_SECONDS", "2")
    assert load_qq_settings(tmp_path / "missing").qq_message_idle_seconds == 2
