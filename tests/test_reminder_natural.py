"""Deferred offline contracts for the finite Chinese reminder dialogue."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from personal_agent.channels.qq.adapter import QQAdapter
from personal_agent.core.calendar import LOCAL_ZONE
from personal_agent.core.history import HistoryError
from personal_agent.scheduler.chat import source_scope
from personal_agent.scheduler.natural import respond
from personal_agent.scheduler.runtime import SchedulerRuntime
from personal_agent.storage import sqlite as storage
from personal_agent.storage.reminder_dialogues import ReminderDialogues
from personal_agent.storage.sqlite import SQLiteStore
from tests.test_qq_adapter import Sender, event, settled, state
from tests.test_qq_transport import config
from tests.test_reminder_chat import attach

__all__ = ["state"]
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=LOCAL_ZONE)


@pytest.fixture
def dialogue(state):
    runtime = attach(state)
    clock = SimpleNamespace(time=NOW)
    runtime.clock = SimpleNamespace(now=lambda: clock.time)
    _, sessions, _, _, inbox = state
    scope = source_scope(inbox.scope)
    return runtime, sessions.current.id, scope, clock


def say(dialogue, text, message="request", **options):
    runtime, conversation, scope, clock = dialogue
    return respond(
        runtime,
        conversation,
        text,
        scope=scope,
        message_id=message,
        received_at=clock.time,
        **options,
    )


@pytest.mark.parametrize(
    ("text", "due"),
    [
        ("明天上午九点提醒我交材料。", NOW.replace(day=31, hour=9)),
        ("请明天下午三点半提醒我喝水", NOW.replace(day=31, hour=15, minute=30)),
        ("提醒我明天 09:15 交材料", NOW.replace(day=31, hour=9, minute=15)),
        ("十分钟后提醒我喝水", NOW + timedelta(minutes=10)),
        ("半小时后提醒我休息", NOW + timedelta(minutes=30)),
        ("下周一上午十点提醒我开会", NOW.replace(month=11, day=2, hour=10)),
    ],
)
def test_explicit_current_request_creates_anchored_schedule(dialogue, text, due):
    response = say(dialogue, text)
    assert "提醒已记录" in response and "尚无到期主动发送渠道" in response
    item = dialogue[0].repository.listing()["items"][0]
    assert datetime.fromisoformat(item["due_at"]) == due
    assert item["source_scope"] == dialogue[2]
    assert say(dialogue, text) == response
    assert len(dialogue[0].repository.listing()["items"]) == 1


def test_missing_fields_survive_restart_and_only_create_when_complete(dialogue):
    runtime, conversation, scope, clock = dialogue
    assert "几点" in say(dialogue, "明天提醒我一下")
    assert not runtime.repository.listing()["items"]
    restarted = SchedulerRuntime(runtime.repository, runtime.policy, clock=runtime.clock)
    dialogue = restarted, conversation, scope, clock
    assert "做什么" in say(dialogue, "上午九点", "clock")
    assert not runtime.repository.listing()["items"]
    assert say(dialogue, "我明天准备跑实验", "ordinary-plan") is None
    assert say(dialogue, "如果明天提醒我就好了", "hypothesis") is None
    assert "提醒已记录" in say(dialogue, "交材料", "body")
    item = runtime.repository.listing()["items"][0]
    assert item["text"] == "交材料"
    assert datetime.fromisoformat(item["due_at"]) == NOW.replace(day=31, hour=9)
    assert ReminderDialogues(runtime.repository).get(conversation, scope, NOW) is None


def test_unqualified_clock_asks_period_and_never_invents_morning(dialogue):
    assert "上午还是" in say(dialogue, "明天九点提醒我交材料")
    assert not dialogue[0].repository.listing()["items"]
    assert "提醒已记录" in say(dialogue, "晚上", "period")
    assert datetime.fromisoformat(dialogue[0].repository.listing()["items"][0]["due_at"]) == (
        NOW.replace(day=31, hour=21)
    )


def test_new_24_hour_answer_overrides_a_previous_period(dialogue):
    assert "几点" in say(dialogue, "明天晚上提醒我交材料")
    assert "提醒已记录" in say(dialogue, "09:00", "precise-clock")
    assert datetime.fromisoformat(dialogue[0].repository.listing()["items"][0]["due_at"]) == (
        NOW.replace(day=31, hour=9)
    )


def test_time_in_reminder_body_does_not_override_an_explicit_prefix(dialogue):
    assert "提醒已记录" in say(dialogue, "明天上午九点提醒我下午交材料")
    item = dialogue[0].repository.listing()["items"][0]
    assert datetime.fromisoformat(item["due_at"]) == NOW.replace(day=31, hour=9)
    assert item["text"] == "下午交材料"


@pytest.mark.parametrize(
    "text",
    [
        "我明天准备跑实验。",
        "如果明天九点提醒我就好了",
        "他说：明天九点提醒我交材料",
        "讨论一下提醒我这个词",
        "“明天上午九点提醒我喝水”",
        "不要明天九点提醒我喝水",
    ],
)
def test_plans_hypotheses_quotes_and_negation_cannot_start_reminder(dialogue, text):
    assert say(dialogue, text) is None
    assert not dialogue[0].repository.listing()["items"]


@pytest.mark.parametrize(
    "text",
    [
        "今天上午九点提醒我交材料",
        "明天25点提醒我交材料",
        "明天上午9点和10点提醒我交材料",
    ],
)
def test_past_invalid_and_multiple_clocks_do_not_create_or_roll_forward(dialogue, text):
    assert say(dialogue, text)
    assert not dialogue[0].repository.listing()["items"]


def test_pending_scope_expiry_abandon_and_unrelated_chat(dialogue):
    runtime, conversation, scope, clock = dialogue
    say(dialogue, "明天提醒我交材料")
    assert say(dialogue, "我今天有点累", "unrelated") is None
    foreign = runtime, conversation, "another-channel", clock
    assert say(foreign, "上午九点") is None
    assert "不设置" in say(dialogue, "算了", "abandon")
    assert say(dialogue, "上午九点", "late") is None
    say(dialogue, "明天提醒我交材料", "new")
    clock.time += timedelta(minutes=16)
    assert say(dialogue, "上午九点", "expired") is None
    assert not runtime.repository.listing()["items"]


def test_list_and_cancel_latest_never_cancel_another_scope(dialogue):
    say(dialogue, "十分钟后提醒我喝水")
    assert "喝水" in say(dialogue, "查看我的提醒", "list")
    foreign = dialogue[0], dialogue[1], "another-channel", dialogue[3]
    assert "没有待取消" in say(foreign, "取消刚才那个提醒", "foreign")
    assert "已取消" in say(dialogue, "取消刚才那个提醒", "cancel")
    assert dialogue[0].repository.listing()["items"][0]["status"] == "cancelled"


def test_generic_cancellation_asks_for_id_without_choosing_arbitrarily(dialogue):
    say(dialogue, "十分钟后提醒我喝水")
    assert "哪条" in say(dialogue, "不用提醒我了", "cancel")
    assert dialogue[0].repository.listing()["items"][0]["status"] == "scheduled"


def test_commit_before_dialogue_cleanup_keeps_original_idempotency_key(dialogue, monkeypatch):
    say(dialogue, "明天提醒我喝水")
    with monkeypatch.context() as patch:

        def fail(*args):
            raise HistoryError("synthetic")

        patch.setattr(ReminderDialogues, "clear", fail)
        with pytest.raises(HistoryError):
            say(dialogue, "上午九点", "clock")
    assert len(dialogue[0].repository.listing()["items"]) == 1
    assert "提醒已记录" in say(dialogue, "上午九点", "clock")
    assert len(dialogue[0].repository.listing()["items"]) == 1


async def test_qq_natural_request_is_standalone_and_calls_no_agent_or_judge(state, dialogue):
    _, _, provider, service, inbox = state
    dialogue[0].clock = SimpleNamespace(now=lambda: datetime.now(UTC))
    adapter = QQAdapter(
        config(qq_message_mode="hybrid", qq_message_judge_idle_seconds=0.001),
        service,
        inbox,
        Sender(),
    )
    adapter.start()
    try:
        await adapter.accept(event("natural", text="明天上午九点提醒我交材料"))
        await settled(adapter)
        await adapter.accept(event("natural", text="明天上午九点提醒我交材料"))
        assert len(dialogue[0].repository.listing()["items"]) == 1
        assert not provider.requests
    finally:
        await adapter.close()


def test_schema_19_to_20_rolls_back_dialogues_and_window_together(tmp_path, monkeypatch):
    path = tmp_path / "natural.db"
    with monkeypatch.context() as patch:
        patch.setattr(storage, "SCHEMA_VERSION", 19)
        with SQLiteStore(path) as db:
            db.initialize()
    with monkeypatch.context() as patch:
        patch.setitem(storage.MIGRATIONS, 20, (*storage.MIGRATIONS[20], "INVALID SQL"))
        with SQLiteStore(path) as db:
            with pytest.raises(HistoryError):
                db.initialize()
            assert db._connection.execute("PRAGMA user_version").fetchone()[0] == 19
            assert (
                db._connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='reminder_dialogues'"
                ).fetchone()
                is None
            )
            columns = db._connection.execute("PRAGMA table_info(proactive_proposals)").fetchall()
            assert "followup_window" not in {row[1] for row in columns}
