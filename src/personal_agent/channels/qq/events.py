"""Project only supported C2C text; never forward the raw event to the model."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from personal_agent.channels.qq.config import QQSettings


@dataclass(frozen=True)
class QQAttachment:
    url: str
    kind: str = "image"


@dataclass(frozen=True)
class QQText:
    message_id: str
    event_id: str
    message_index: str
    openid: str
    platform_at: datetime
    received_at: datetime
    text: str
    attachments: tuple[QQAttachment, ...] = ()


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
    if type(message_type) is not int or message_type not in {0, 3}:
        return None
    if data.get("msg_elements"):
        return None
    raw_attachments = data.get("attachments") or []
    card = data.get("ark_data")
    if card or message_type == 3:
        if not isinstance(card, dict) or card.get("ark_type") != "picture":
            return None
        fields = card.get("fields")
        if not isinstance(fields, dict) or not isinstance(fields.get("preview"), str):
            return None
        # Only the documented picture preview is projected; no other card fields or URLs.
        raw_attachments = [{"content_type": "image", "url": fields["preview"]}]
    if not isinstance(raw_attachments, list):
        return None
    attachments = []
    if len(raw_attachments) > settings.qq_image_max_count:
        attachments = [QQAttachment("", "unsupported")]
    else:
        for item in raw_attachments:
            if not isinstance(item, dict) or not isinstance(item.get("content_type"), str):
                return None
            kind = item["content_type"].split(";", 1)[0].lower().strip()
            if kind not in {"image", "image/jpeg", "image/png", "image/gif", "image/webp"}:
                attachments.append(QQAttachment("", "unsupported"))
                continue
            url = item.get("url")
            if not isinstance(url, str) or not 1 <= len(url) <= 8192:
                attachments.append(QQAttachment("", "unsupported"))
            else:
                attachments.append(QQAttachment(url))
    text, message_id = data.get("content"), data.get("id")
    event_id = payload.get("id", "")
    if text is None and attachments:
        text = ""
    if not isinstance(text, str) or (not text.strip() and not attachments):
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
    return QQText(message_id, event_id, index, openid, stamp, now, text.strip(), tuple(attachments))


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
