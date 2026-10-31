"""Deferred offline social-window contracts: sampled timing never schedules a reminder."""

from datetime import datetime, timedelta

import pytest

from personal_agent.config import Settings
from personal_agent.core.calendar import LOCAL_ZONE
from personal_agent.proactive.models import ProactivePolicy
from personal_agent.proactive.timing import followup_window
from tests.test_episodes import seed, state
from tests.test_proactive import proactive

__all__ = ["state"]
NOW = datetime(2026, 10, 30, 12, tzinfo=LOCAL_ZONE)


def test_day_only_uses_persistable_non_round_evening_time():
    policy = ProactivePolicy()
    window = followup_window("我明天准备跑实验。", NOW, "agent:experiment:1", policy)
    review = datetime.fromisoformat(window["review_at"])
    assert review.date() == (NOW + timedelta(days=1)).date()
    assert review.replace(hour=19, minute=0, second=0) < review
    assert review < review.replace(hour=21, minute=30, second=0)
    assert review.second != 0 and window["phase"] == "evening"
    assert followup_window("我明天准备跑实验。", NOW, "agent:experiment:1", policy) == window
    assert followup_window("我明天准备跑实验。", NOW, "agent:experiment:2", policy) != window


def test_important_event_and_probability_have_bounded_pre_event_windows():
    policy = ProactivePolicy(before_probability=0)
    important = followup_window("明天上午九点有重要的实验", NOW, "important", policy)
    event = datetime.fromisoformat(important["event_at"])
    lead = event - datetime.fromisoformat(important["review_at"])
    assert important["phase"] == "before"
    assert timedelta(minutes=15) < lead < timedelta(minutes=45)
    ordinary = followup_window("明天上午九点跑实验", NOW, "ordinary", policy)
    assert ordinary["phase"] == "evening"
    policy.before_probability = 1
    selected = followup_window("明天上午九点跑实验", NOW, "ordinary", policy)
    assert selected["phase"] == "before"


def test_late_event_never_gets_an_afterwards_question_before_start():
    window = followup_window(
        "明天晚上十点跑实验", NOW, "late", ProactivePolicy(before_probability=0)
    )
    assert window["phase"] == "evening"
    assert datetime.fromisoformat(window["start"]) > datetime.fromisoformat(window["event_at"])


def test_no_date_ambiguous_clock_invalid_time_and_disable_do_not_invent_windows():
    policy = ProactivePolicy()
    for quote in ["准备跑实验", "九点跑实验", "明天25点跑实验", "明天上午9点和10点跑实验"]:
        assert followup_window(quote, NOW, "unclear", policy) is None
    policy.social_followup_enabled = False
    assert followup_window("我明天跑实验", NOW, "off", policy) is None


def test_social_configuration_is_forwarded_and_bad_windows_fail():
    settings = Settings(api_key="synthetic", model="synthetic", context_window=131072)
    assert settings.proactive_policy().social_followup_enabled
    with pytest.raises(ValueError):
        ProactivePolicy(evening_start_minute=1300, evening_end_minute=1200)


async def test_window_persists_across_restart_and_no_scheduler_records(state):
    store, conversation, _ = state
    service = proactive(store, social_followup_enabled=True)
    seed(store, conversation, "我明天准备跑实验。")
    assert (await service.run())["stage"] == "scout"
    proposal = service.repository.candidates()[0]
    assert proposal["followup_window"]["phase"] == "evening"
    assert (await service.run())["outcome"] == "no_proactive_work"
    restarted = proactive(store, social_followup_enabled=True)
    assert restarted.repository.candidates()[0]["next_review_at"] == proposal["next_review_at"]
    assert store._connection.execute("SELECT COUNT(*) FROM scheduled_reminders").fetchone()[0] == 0
    restarted.clock.time = datetime.fromisoformat(proposal["next_review_at"])
    assert (await restarted.run())["stage"] == "attention"
    assert restarted.repository.listing("decisions")["items"][0]["payload"]["mode"] == "shadow"


async def test_cancel_before_window_prevents_attention_and_missed_window_expires(state):
    store, conversation, _ = state
    service = proactive(store, social_followup_enabled=True)
    seed(store, conversation, "我明天准备跑实验。")
    await service.run()
    proposal = service.repository.candidates()[0]
    service.model.provider.status = "dismissed"
    seed(store, conversation, "实验取消了，明天不做了")
    await service.run()
    service.clock.time = datetime.fromisoformat(proposal["next_review_at"])
    assert (await service.run())["outcome"] == "no_proactive_work"
    assert service.repository.candidates()[0]["status"] == "dismissed"


async def test_shutdown_past_window_does_not_generate_late_draft(state):
    store, conversation, _ = state
    service = proactive(store, social_followup_enabled=True)
    seed(store, conversation, "我明天准备跑实验。")
    await service.run()
    item = service.repository.candidates()[0]
    service.clock.time = datetime.fromisoformat(item["expires_at"]) + timedelta(seconds=1)
    assert (await service.run())["stage"] == "expiry"
    assert service.repository.candidates()[0]["status"] == "expired"
    assert len(service.model.provider.requests) == 1
