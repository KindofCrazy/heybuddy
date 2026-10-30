"""Only explicit friend messages from the configured peer become journal input."""

import re
from datetime import UTC, datetime

from personal_agent.channels.napcat.config import NapCatSettings
from personal_agent.channels.qq.config import QQSettings
from personal_agent.channels.qq.events import QQAttachment, QQText


def numeric_id(value: object) -> str | None:
    if type(value) not in {int, str}:
        return None
    text = str(value)
    return text if re.fullmatch(r"-?[0-9]{1,20}", text) else None


def parse_input_status(payload: dict, account: NapCatSettings) -> tuple[int, float | None] | None:
    """Presence is a scheduling hint, never journal text or user evidence."""
    if (
        not account.napcat_input_typing_enabled
        or payload.get("post_type") != "notice"
        or payload.get("notice_type") != "notify"
        or payload.get("sub_type") != "input_status"
        or numeric_id(payload.get("self_id")) != account.napcat_self_id
        or numeric_id(payload.get("user_id")) != account.napcat_peer_id
        or numeric_id(payload.get("group_id", 0)) != "0"
    ):
        return None
    stamp, event_type, text = (
        payload.get("time"),
        payload.get("event_type"),
        payload.get("status_text"),
    )
    if type(stamp) is not int or type(event_type) is not int or not 0 <= event_type <= 32:
        return None
    if not isinstance(text, str):
        return None
    try:
        if len(text.encode("utf-8")) > 256:
            return None
        age = datetime.now(UTC).timestamp() - stamp
        if not -1 <= age <= account.napcat_input_typing_ttl_seconds:
            return None
    except (UnicodeError, OverflowError):
        return None
    return stamp, stamp + account.napcat_input_typing_ttl_seconds if text.strip() else None


def parse_message(payload: dict, account: NapCatSettings, policy: QQSettings) -> QQText | None:
    if (
        payload.get("post_type") != "message"
        or payload.get("message_type") != "private"
        or payload.get("sub_type") != "friend"
        or numeric_id(payload.get("self_id")) != account.napcat_self_id
        or numeric_id(payload.get("user_id")) != account.napcat_peer_id
    ):
        return None
    sender = payload.get("sender")
    if not isinstance(sender, dict) or numeric_id(sender.get("user_id")) != account.napcat_peer_id:
        return None
    message_id = numeric_id(payload.get("message_id"))
    segments = payload.get("message")
    if message_id is None or not isinstance(segments, list) or not 1 <= len(segments) <= 128:
        return None
    texts, attachments = [], []
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
            return None
        data = segment["data"]
        kind = segment.get("type")
        if not isinstance(kind, str):
            return None
        if kind == "text":
            if not isinstance(data.get("text"), str):
                return None
            texts.append(data["text"])
        elif kind == "image":
            url = data.get("url")
            attachments.append(
                QQAttachment(url)
                if isinstance(url, str) and 1 <= len(url) <= 8192
                else QQAttachment("", "unsupported")
            )
        elif kind in {"reply", "face"}:
            # No expansion of quoted messages or fetching of expressions.
            if kind == "face":
                texts.append("[QQ 表情]")
        else:
            # Never silently reinterpret files, voice, cards or forwarded messages as text.
            return None
    text = "".join(texts).strip()
    try:
        if len(text.encode("utf-8")) > policy.qq_input_max_bytes:
            return None
        stamp = payload.get("time")
        if type(stamp) is not int:
            return None
        platform_at = datetime.fromtimestamp(stamp, UTC)
    except (ValueError, UnicodeError, OverflowError, OSError):
        return None
    now = datetime.now(UTC)
    if (platform_at - now).total_seconds() > 30 or (not text and not attachments):
        return None
    if len(attachments) > policy.qq_image_max_count:
        attachments = [QQAttachment("", "unsupported")]
    return QQText(
        message_id, "", "", account.napcat_peer_id, platform_at, now, text, tuple(attachments)
    )
