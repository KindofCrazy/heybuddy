import time
from datetime import UTC, datetime

import pytest

from personal_agent.channels.napcat.events import parse_input_status
from personal_agent.channels.qq.adapter import QQAdapter
from tests.test_napcat import account
from tests.test_qq_adapter import Sender, event, rows, settled, state
from tests.test_qq_transport import config
from tests.test_qq_turns import receive

__all__ = ["state"]


def notice(**changes):
    return {
        "time": int(datetime.now(UTC).timestamp()),
        "self_id": 1234567,
        "post_type": "notice",
        "notice_type": "notify",
        "sub_type": "input_status",
        "event_type": 2,
        "user_id": 7654321,
        "group_id": 0,
        "status_text": "对方正在输入...",
        **changes,
    }


def test_input_status_projection_accepts_presence_and_empty_stop():
    payload = notice()
    assert parse_input_status(payload, account()) == (payload["time"], payload["time"] + 30)
    assert parse_input_status(notice(status_text=""), account())[1] is None
    assert parse_input_status(payload, account(napcat_input_typing_enabled=False)) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"user_id": 1234567},
        {"user_id": 8888888},
        {"self_id": 8888888},
        {"group_id": 123},
        {"sub_type": "poke"},
        {"post_type": "message"},
        {"notice_type": "group"},
        {"event_type": True},
        {"status_text": []},
        {"status_text": "\ud800"},
        {"status_text": "字" * 1000},
        {"time": 1},
        {"time": 10**100},
    ],
)
def test_input_status_ignores_foreign_stale_and_malformed_notices(changes):
    assert parse_input_status(notice(**changes), account()) is None


def adapter_for(state):
    db, sessions, provider, service, inbox = state
    adapter = QQAdapter(
        config(qq_message_idle_seconds=2.5, qq_message_max_wait_seconds=8), service, inbox, Sender()
    )
    return adapter, db, sessions, provider, inbox


def test_typing_bridges_idle_gap_and_merges_followup_without_journal_evidence(state, monkeypatch):
    adapter, db, sessions, provider, inbox = adapter_for(state)
    base = receive(inbox, sessions.current.id, 1, 0, "先说一点")
    monkeypatch.setattr("personal_agent.channels.qq.adapter.time.time", lambda: base + 2)
    adapter._hybrid.ready = (1,)
    adapter.note_typing(base + 7)
    assert adapter._hybrid.ready == ()
    assert (
        inbox.claim_turn(
            sessions.current.id,
            adapter.settings,
            now=base + 3,
            extend_after=adapter._turn_extensions,
        )[1]
        == 4
    )
    assert rows(db)[0]["status"] == "received" and not provider.requests
    receive(inbox, sessions.current.id, 2, 4, "还有后半句")
    turn, delay = inbox.claim_turn(
        sessions.current.id, adapter.settings, now=base + 6.5, extend_after=adapter._turn_extensions
    )
    assert delay == 0 and turn["content"] == "先说一点\n还有后半句"
    assert len(db.list_runs(sessions.current.id)) == 0


def test_refresh_cannot_move_first_message_maximum_or_change_existing_run(state, monkeypatch):
    adapter, db, sessions, _, inbox = adapter_for(state)
    base = receive(inbox, sessions.current.id, 1, 0)
    monkeypatch.setattr("personal_agent.channels.qq.adapter.time.time", lambda: base + 7)
    adapter.note_typing(base + 100)
    row, delay = inbox.claim_turn(
        sessions.current.id, adapter.settings, now=base + 8, extend_after=adapter._turn_extensions
    )
    assert delay == 0 and row is not None
    previous = adapter._turn_extensions.copy()
    adapter.note_typing(base + 200)
    assert adapter._turn_extensions == previous
    assert rows(db)[0]["status"] == "processing"


def test_stop_restores_quiet_wait_and_expired_hint_does_not_wait_forever(state, monkeypatch):
    adapter, _, sessions, _, inbox = adapter_for(state)
    base = receive(inbox, sessions.current.id, 1, 0)
    monkeypatch.setattr("personal_agent.channels.qq.adapter.time.time", lambda: base + 3)
    adapter.note_typing(base + 7)
    adapter.note_typing(None)
    assert adapter._turn_extensions[1] == base + 5.5
    assert (
        inbox.claim_turn(
            sessions.current.id,
            adapter.settings,
            now=base + 5.5,
            extend_after=adapter._turn_extensions,
        )[0]
        is not None
    )


def test_typing_without_text_or_before_command_creates_no_turn(state):
    adapter, db, sessions, provider, inbox = adapter_for(state)
    adapter.note_typing(datetime.now(UTC).timestamp() + 5)
    assert not adapter._turn_extensions and not rows(db)
    receive(inbox, sessions.current.id, 1, 0, "/memory edit")
    adapter.note_typing(datetime.now(UTC).timestamp() + 5)
    assert not adapter._turn_extensions and not provider.requests


async def test_active_notice_blocks_early_hybrid_ready_but_still_runs_at_hard_maximum(state):
    _, _, provider, service, inbox = state
    policy = config(
        qq_message_mode="hybrid",
        qq_message_idle_seconds=0.01,
        qq_message_max_wait_seconds=0.05,
        qq_message_judge_idle_seconds=0.005,
    )
    adapter = QQAdapter(policy, service, inbox, Sender())
    await adapter.accept(event())
    adapter.note_typing(time.time() + 100)
    adapter.start()
    try:
        await settled(adapter)
        assert len(provider.requests) == 1  # No early model turn judge while the peer is typing.
        assert not adapter._turn_extensions
    finally:
        await adapter.close()
