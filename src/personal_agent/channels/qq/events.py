"""Project only supported C2C text; never forward the raw event to the model."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from personal_agent.channels.qq.config import QQSettings


@dataclass(frozen=True)
class QQText:
    message_id: str
    event_id: str
    message_index: str
    openid: str
    platform_at: datetime
    received_at: datetime
    text: str


def parse_text(payload: dict, settings: QQSettings) -> QQText | None:
    if payload.get("op") != 0 or payload.get("t") != "C2C_MESSAGE_CREATE":
        return None
    data = payload.get("d")
    if not isinstance(data, dict):
        return None
    author = data.get("author")
    if not isinstance(author, dict) or author.get("bot", False):
        return None
    openid = author.get("user_openid")
    if not isinstance(openid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", openid):
        return None
    message_type = data.get("message_type", 0)
    if type(message_type) is not int or message_type != 0:
        return None
    if any(data.get(key) for key in ("attachments", "ark_data", "msg_elements")):
        return None
    text, message_id = data.get("content"), data.get("id")
    event_id = payload.get("id", "")
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        if len(text.encode("utf-8")) > settings.qq_input_max_bytes:
            return None
    except UnicodeError:
        return None
    if not isinstance(message_id, str) or not re.fullmatch(r"[!-~]{1,512}", message_id):
        return None
    if not isinstance(event_id, str) or (event_id and not re.fullmatch(r"[!-~]{1,512}", event_id)):
        return None
    try:
        stamp = datetime.fromisoformat(data["timestamp"])
        if stamp.tzinfo is None:
            return None
        stamp = stamp.astimezone(UTC)
    except (KeyError, ValueError, TypeError, OverflowError):
        return None
    now = datetime.now(UTC)
    if (stamp - now).total_seconds() > 30:
        return None
    scene = data.get("message_scene", {})
    ext = scene.get("ext", []) if isinstance(scene, dict) else []
    index = ""
    if isinstance(ext, list):
        for item in ext[:32]:
            if isinstance(item, str) and item.startswith("msg_idx=") and len(item) <= 512:
                index = item.removeprefix("msg_idx=")
                break
    return QQText(message_id, event_id, index, openid, stamp, now, text.strip())


def split_reply(text: str, max_bytes: int, max_parts: int) -> list[str]:
    """Bound visible delivery, prefer natural breaks, preserve Unicode code points."""
    remaining = text.strip() or "这次没有生成可发送的文本，请再发一条消息。"
    parts = []
    notice = "\n（内容较长，余下部分请继续询问。）"
    for index in range(max_parts):
        limit = max_bytes
        truncated = len(remaining.encode("utf-8")) > limit
        if truncated and index == max_parts - 1:
            limit -= len(notice.encode("utf-8"))
        chunk = remaining.encode("utf-8")[:limit].decode("utf-8", errors="ignore")
        if len(chunk) < len(remaining) and index != max_parts - 1:
            for separators in ("\n", "。！？.!? "):
                boundary = max(chunk.rfind(separator) for separator in separators)
                if boundary >= len(chunk) // 2:
                    chunk = chunk[: boundary + 1]
                    break
        remaining = remaining[len(chunk) :].lstrip()
        if truncated and index == max_parts - 1:
            chunk += notice
        parts.append(chunk)
        if not remaining:
            break
    return parts
