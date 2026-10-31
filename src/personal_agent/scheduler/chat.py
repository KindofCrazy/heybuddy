"""Narrow reminder commands for a channel-validated peer; never invokes the Agent."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

from personal_agent.scheduler.cli import command
from personal_agent.scheduler.models import ReminderError
from personal_agent.scheduler.runtime import SchedulerRuntime

HELP = (
    "可以发送：\n/remind in 10m 喝水\n"
    "/remind at 2026-10-31T09:00:00+08:00 提交论文\n"
    "/remind list\n/remind show <提醒ID>\n/remind cancel <提醒ID>\n"
    "保存记录不表示已经投递，具体渠道能力以设置回执为准。"
)
STATUS = {
    "scheduled": "等待到期",
    "blocked": "已到期，发送渠道不可用",
    "sending": "投递处理中",
    "accepted": "模拟投递已接受",
    "failed": "投递失败",
    "uncertain": "投递结果不确定",
    "cancelled": "已取消",
    "expired": "已过期",
}


def source_scope(scope: tuple[str, str, str]) -> str:
    raw = json.dumps(scope, ensure_ascii=False, separators=(",", ":"))
    return "chat:" + hashlib.sha256(raw.encode()).hexdigest()


def request_key(scope: str, message_id: str) -> str:
    raw = json.dumps([scope, message_id], ensure_ascii=False, separators=(",", ":"))
    return "chat:" + hashlib.sha256(raw.encode()).hexdigest()


def when(value: str) -> str:
    local = datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=8)))
    return local.strftime("%Y-%m-%d %H:%M:%S（UTC+8）")


def describe(item: dict) -> str:
    status = STATUS.get(item["status"], item["status"])
    if item["status"] == "accepted" and item["mode"] == "napcat":
        status = "渠道已接受（不代表已读）"
    return f"ID：{item['id']}\n时间：{when(item['due_at'])}\n状态：{status}\n内容：{item['text']}"


def delivery_notice(runtime: SchedulerRuntime, conversation: str, scope: str) -> str:
    if runtime.delivery_for(conversation, scope):
        return "当前 NapCat 提醒渠道可用，将在到期时尝试投递；应用离线时不会保证通知。"
    return "尚无到期主动发送渠道，不保证准时通知。"


async def reply(
    runtime: SchedulerRuntime,
    conversation_id: str,
    text: str,
    *,
    scope: str,
    message_id: str,
    received_at: datetime,
    has_attachments: bool,
) -> str:
    if runtime.read_only:
        raise ReminderError("scheduler_unavailable")
    parts = text.strip().split(maxsplit=3)
    if not parts or parts[0] != "/remind":
        raise ReminderError("reminder_invalid_command")
    if has_attachments:
        return "提醒命令暂只接受纯文本，请将图片与命令分开发送。"
    args = text.strip()[len("/remind") :].strip()
    if len(parts) == 1 or parts[1] == "help":
        return HELP
    action = parts[1]
    if action not in {"at", "in", "list", "show", "cancel"}:
        return "聊天中只支持提醒的创建、列表、查看和取消。\n" + HELP
    if action == "list":
        if len(parts) > 3:
            return HELP
        try:
            cursor = int(parts[2]) if len(parts) == 3 else 2**63 - 1
            data = runtime.repository.listing(
                5, cursor, conversation_id=conversation_id, source_scope=scope
            )
        except ValueError:
            raise ReminderError("reminder_invalid_arguments") from None
        if not data["items"]:
            return "当前聊天还没有提醒记录。"
        lines = ["当前聊天的提醒："]
        for item in data["items"]:
            lines.append(describe({**item, "text": item["text"][:160]}))
        if data["next_cursor"] is not None:
            lines.append(f"继续查看：/remind list {data['next_cursor']}")
        return "\n\n".join(lines)
    if action in {"show", "cancel"}:
        if len(parts) != 3:
            return HELP
        item = runtime.repository.get(parts[2])
        if item["conversation_id"] != conversation_id or item["source_scope"] != scope:
            raise ReminderError("reminder_not_found")
        if action == "show":
            return describe(item)
        result = runtime.cancel(item["id"])
        prefix = {
            "cancelled": "提醒已取消。",
            "too_late": "已开始投递，无法保证撤回。",
            "terminal": "该提醒已结束，当前没有可取消的排期。",
            "expired": "该提醒已经过期。",
        }
        return prefix[result["outcome"]] + "\n" + describe(result["reminder"])
    result = await command(
        runtime,
        conversation_id,
        args,
        request_id=request_key(scope, message_id),
        reference_at=received_at,
        source_scope=scope,
    )
    if result.get("outcome") != "recorded":
        return HELP
    return (
        "提醒记录：\n"
        + describe(result["reminder"])
        + "\n"
        + delivery_notice(runtime, conversation_id, scope)
    )
