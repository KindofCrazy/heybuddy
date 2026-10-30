import pytest
from pydantic import ValidationError

from personal_agent.channels.napcat.config import NapCatSettings
from personal_agent.channels.qq.config import QQSettings
from personal_agent.channels.qq.humanize import Humanizer
from tests.test_napcat import account


def test_napcat_quota_and_rhythm_ignore_official_delivery_settings():
    settings = account()
    policy = settings.delivery_settings(
        QQSettings(
            qq_reply_max_parts=1,
            qq_text_max_bytes=128,
            qq_reply_window_seconds=60,
            qq_humanize_enabled=False,
            qq_send_interval_seconds=10,
        )
    )
    assert (policy.qq_reply_max_parts, policy.qq_text_max_bytes) == (32, 8000)
    assert policy.qq_reply_window_seconds == 900 and policy.qq_send_interval_seconds == 0.3
    assert policy.qq_humanize_enabled
    assert policy.qq_humanize_ms_per_char == 25 and policy.qq_humanize_gap_seconds == 0.4
    with pytest.raises(ValidationError):
        QQSettings(qq_reply_max_parts=32)


def test_natural_bubbles_exceed_four_and_do_not_clip_long_content():
    policy = account().delivery_settings(QQSettings())
    human = Humanizer(policy, length_based_gaps=True)
    short = "\n\n".join(f"这是第{i}段内容。" for i in range(12))
    parts = human.split(short)
    assert len(parts) == 12 and "第11段" in parts[-1]
    text = "汉字🙂" * 8000
    parts = human.split(text)
    assert 8 < len(parts) <= 32
    assert "".join(parts) == text
    assert all(len(part.encode()) <= 8000 for part in parts)
    assert "余下部分" not in "".join(parts)


def test_each_bubble_delay_depends_on_its_own_length_and_has_a_cap():
    policy = account(napcat_humanize_jitter=0).delivery_settings(QQSettings())
    human = Humanizer(policy, length_based_gaps=True)
    assert human.delay("字" * 10, first=False) == 0.4
    assert human.delay("字" * 40, first=False) == 1
    assert human.delay("字" * 100, first=False) == 2.5
    assert human.delay("字" * 1000, first=False) == 3
    assert human.delay("字" * 100, first=True, elapsed=2) == 0.5
    assert human.delay("字" * 100, first=True, elapsed=5) == 0
    assert Humanizer(QQSettings(qq_humanize_jitter=0)).delay("字" * 100, first=False) == 1.2


def test_structure_and_disabled_humanize_keep_single_large_message():
    text = "```python\n" + "print('你好')\n" * 120 + "```"
    policy = account().delivery_settings(QQSettings())
    assert len(text.encode()) > 1500
    assert Humanizer(policy).split(text) == [text]
    policy = account(napcat_humanize_enabled=False).delivery_settings(QQSettings())
    assert Humanizer(policy).split("在呀\n\n今天怎么样") == ["在呀\n\n今天怎么样"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"napcat_reply_max_parts": 65},
        {"napcat_text_max_bytes": 16385},
        {"napcat_input_typing_ttl_seconds": 0.1},
        {"napcat_input_typing_ttl_seconds": float("inf")},
        {"napcat_humanize_jitter": 1},
    ],
)
def test_new_settings_remain_bounded(kwargs):
    with pytest.raises(ValidationError):
        NapCatSettings(**kwargs)
