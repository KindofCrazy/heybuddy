import pytest
from pydantic import ValidationError

from personal_agent.channels.qq.config import QQSettings
from personal_agent.channels.qq.events import split_reply
from personal_agent.channels.qq.humanize import Humanizer
from personal_agent.config import load_qq_settings, load_settings


def test_natural_bubbles_keep_short_fragments_and_overflow_together():
    human = Humanizer(QQSettings())
    assert human.split("今天终于跑通了？\n之前卡住的地方，最后怎么解决的？") == [
        "今天终于跑通了？",
        "之前卡住的地方，最后怎么解决的？",
    ]
    assert human.split("嗯。好。继续说说看。") == ["嗯。好。继续说说看。"]
    assert human.split(
        "第一段聊聊今天。\n第二段聊聊昨天。\n第三段聊聊明天。\n第四段聊聊周末。\n第五段先留着。"
    ) == [
        "第一段聊聊今天。",
        "第二段聊聊昨天。",
        "第三段聊聊明天。",
        "第四段聊聊周末。\n第五段先留着。",
    ]


@pytest.mark.parametrize("separator", ["\n\n", "\r\n\r\n", "\n \t\n\n"])
@pytest.mark.parametrize("greeting", ["在呀", "在呀。", "嗯。好。"])
def test_blank_lines_preserve_short_bubbles(separator, greeting):
    text = greeting + separator + "怎么，睡不着吗"
    assert Humanizer(QQSettings()).split(text) == [greeting, "怎么，睡不着吗"]


def test_paragraph_bubbles_keep_overflow_and_respect_disabled_delivery():
    text = "在呀\n\n嗯\n\n怎么了\n\n睡不着吗\n\n聊聊吧"
    assert Humanizer(QQSettings()).split(text) == [
        "在呀",
        "嗯",
        "怎么了",
        "睡不着吗\n\n聊聊吧",
    ]
    assert Humanizer(QQSettings(qq_reply_max_parts=1)).split(text) == [text]
    assert Humanizer(QQSettings(qq_humanize_enabled=False)).split(text) == [text]


@pytest.mark.parametrize(
    "text",
    [
        "代码如下：\n```python\nprint('hello!')\nprint('world!')\n```",
        "代码如下：\n~~~python\nx = 1\n~~~",
        "先试试 `a.b()`。\n再看结果。",
        "# 操作步骤\n第一步。\n第二步。",
        "操作步骤：\n- 检查输入。\n- 检查输出。",
        "操作步骤：\n1. 检查输入。\n2. 检查输出。",
        "| 输入 | 输出 |\n| --- | --- |\n| a | b |",
        "说明如下：\n> 原始引文。\n> 继续引文。",
        "说明如下：\n    print('a!')\n    print('b!')",
        "看这里：https://example.test/a?q=hi!\n然后继续。",
        "看这里：[参考](relative/path)。\n然后继续。",
        "计算 $a+b$。\n再求导。",
        "计算 \\(a+b\\)。\n再求导。",
    ],
)
def test_structured_answers_receive_no_additional_splits(text):
    assert Humanizer(QQSettings()).split(text) == [text]


@pytest.mark.parametrize("max_parts", [1, 2, 3, 4])
@pytest.mark.parametrize(
    "text",
    [
        "",
        "你好🙂",
        "在呀。\r\n \t\r\n怎么，睡不着吗\n\n" * 50,
        "汉字🙂。\n\n" * 1000,
        "a" * 10000,
        "第一句讲得稍微长一些。 第二句也讲得稍微长一些！\n" * 8,
        "字" * 42 + "。\n短句。\n" + "字" * 70,
    ],
)
def test_natural_splits_do_not_lose_capacity_or_exceed_byte_and_part_limits(text, max_parts):
    settings = QQSettings(qq_text_max_bytes=128, qq_reply_max_parts=max_parts)
    parts = Humanizer(settings).split(text)
    baseline = split_reply(text, 128, max_parts)
    assert 1 <= len(parts) <= max_parts
    assert all(0 < len(part.encode()) <= 128 and "\ufffd" not in part for part in parts)
    assert "".join("".join(parts).split()) == "".join("".join(baseline).split())


def test_disable_restores_original_delivery_plan_and_zero_pause():
    text = "今天终于跑通了？\n最后怎么解决的？"
    human = Humanizer(QQSettings(qq_humanize_enabled=False))
    assert human.split(text) == [text]
    assert human.delay(text, first=True) == human.delay(text, first=False) == 0


def test_delays_credit_generation_time_and_clamp_after_jitter(monkeypatch):
    monkeypatch.setattr("personal_agent.channels.qq.humanize.random.uniform", lambda a, b: b)
    human = Humanizer(QQSettings())
    assert human.delay("字" * 1000, first=True) == 1.5
    assert human.delay("字" * 1000, first=True, elapsed=1) == 0.5
    assert human.delay("字" * 1000, first=True, elapsed=10) == 0
    assert human.delay("短句", first=False) == pytest.approx(1.44)
    assert human.delay("短句", first=False, elapsed=10) == pytest.approx(1.44)
    zero = Humanizer(QQSettings(qq_humanize_max_delay_seconds=0))
    assert zero.delay("测试", first=True) == zero.delay("测试", first=False) == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("qq_humanize_ms_per_char", -1),
        ("qq_humanize_ms_per_char", 151),
        ("qq_humanize_max_delay_seconds", 5.1),
        ("qq_humanize_gap_seconds", -1),
        ("qq_humanize_jitter", 0.6),
        ("qq_humanize_jitter", float("nan")),
    ],
)
def test_delivery_settings_are_bounded(field, value):
    with pytest.raises(ValidationError):
        QQSettings(**{field: value})


def test_delivery_environment_options_reach_both_loaders(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    monkeypatch.setenv("OPENAI_MODEL", "synthetic")
    monkeypatch.setenv("AGENT_CONTEXT_WINDOW", "64000")
    options = {
        "ENABLED": "false",
        "MS_PER_CHAR": "12",
        "MAX_DELAY_SECONDS": "0.8",
        "GAP_SECONDS": "0.3",
        "JITTER": "0.1",
    }
    for suffix, value in options.items():
        monkeypatch.setenv("AGENT_QQ_HUMANIZE_" + suffix, value)
    for settings in (load_settings(), load_qq_settings()):
        assert settings.qq_humanize_enabled is False
        assert settings.qq_humanize_ms_per_char == 12
        assert settings.qq_humanize_max_delay_seconds == 0.8
        assert settings.qq_humanize_gap_seconds == 0.3
        assert settings.qq_humanize_jitter == 0.1
