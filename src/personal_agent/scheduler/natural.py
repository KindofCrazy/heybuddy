"""Finite Chinese reminder dialogue; only explicit current requests can start a draft."""

import re
from datetime import date, datetime, timedelta

from pydantic import AwareDatetime, Field

from personal_agent.core.calendar import LOCAL_ZONE, clock_hour, parse_time
from personal_agent.core.evidence import SECRET
from personal_agent.core.models import Model
from personal_agent.core.reminder_intent import CREATE, META, UNTRUSTED, reminder_action
from personal_agent.scheduler.chat import describe, request_key
from personal_agent.scheduler.models import ReminderError, ReminderRequest
from personal_agent.scheduler.runtime import SchedulerRuntime
from personal_agent.storage.reminder_dialogues import ReminderDialogues


class Draft(Model):
    request_id: str = Field(min_length=1, max_length=128)
    anchor: AwareDatetime
    day: date | None = None
    hour: int | None = Field(default=None, ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    period: str | None = None
    clock_24h: bool = False
    due_at: AwareDatetime | None = None
    text: str = Field(default="", max_length=1200)


def _body(text: str) -> str:
    return re.sub(r"^[\s，,：:]*(?:在|于)?\s*", "", text).strip(" ，,。！!？?")


def _when(due_at: datetime, now: datetime) -> str:
    seconds = round((due_at - now).total_seconds())
    if 0 < seconds <= 3600:
        if seconds % 3600 == 0:
            return f"{seconds // 3600}小时后"
        if seconds % 60 == 0:
            return f"{seconds // 60}分钟后"
        if seconds < 60:
            return f"{seconds}秒后"
    due, current = due_at.astimezone(LOCAL_ZONE), now.astimezone(LOCAL_ZONE)
    days = (due.date() - current.date()).days
    day = {0: "今天", 1: "明天", 2: "后天"}.get(days)
    if day is None:
        day = f"{due.month}月{due.day}日"
        if due.year != current.year:
            day = f"{due.year}年" + day
    clock = due.strftime("%H:%M:%S" if due.second else "%H:%M")
    return day + clock


def _prompt(draft: Draft) -> str | None:
    if draft.due_at is None:
        if draft.hour is None:
            return "想让我几点提醒你？可以说“明天上午九点”，也可以说“十分钟后”。"
        try:
            hour = clock_hour(draft.hour, draft.period, draft.clock_24h)
        except ValueError:
            return "这个时刻有歧义，请明确日期和上午、下午或晚上，例如“明天晚上九点”。"
        if hour is None:
            return "你说的是上午还是下午、晚上？也可以用 24 小时制，例如 09:00。"
        day = draft.day or draft.anchor.astimezone(LOCAL_ZONE).date()
        draft.due_at = datetime.combine(day, datetime.min.time(), LOCAL_ZONE).replace(
            hour=hour, minute=draft.minute
        )
    if not draft.text:
        return "要提醒你做什么？请补充具体事项。"
    return None


def _cancel(
    runtime: SchedulerRuntime, conversation: str, scope: str, now: datetime, action: str
) -> str:
    dialogs = ReminderDialogues(runtime.repository)
    pending = dialogs.get(conversation, scope, now)
    if pending is not None:
        dialogs.clear(conversation, scope)
        return "好的，刚才还没设置完成的提醒已取消，没有新增排期。"
    page = runtime.repository.listing(20, conversation_id=conversation, source_scope=scope)
    active = [r for r in page["items"] if r["status"] in {"scheduled", "blocked"}]
    if not active:
        return "当前聊天最近的记录中没有待取消的提醒。可以用 /remind list 查看。"
    latest = page["items"][0]
    age = now - datetime.fromisoformat(latest["created_at"])
    recent = timedelta(0) <= age <= timedelta(minutes=30)
    if action != "cancel_latest" or not recent:
        choices = "\n".join(f"{r['id']}：{r['text'][:80]}" for r in active[:3])
        return "你想取消哪条提醒？请用 /remind cancel <提醒ID> 指定：\n" + choices
    if latest["status"] not in {"scheduled", "blocked"}:
        return "最近一条提醒已结束，我没有取消其他提醒。可以用 /remind list 查看。"
    result = runtime.cancel(latest["id"])
    if result["outcome"] == "cancelled":
        return f"好，已取消“{result['reminder']['text']}”的提醒。"
    return "这条提醒当前无法取消，可以查看提醒记录确认状态。"


def respond(
    runtime: SchedulerRuntime,
    conversation: str,
    text: str,
    *,
    scope: str,
    message_id: str,
    received_at: datetime,
    has_attachments: bool = False,
) -> str | None:
    """Synchronous disk steps cannot be interrupted between publication and route marking."""
    action = reminder_action(text)
    if runtime.read_only or text.lstrip().startswith("/"):
        return None
    dialogs = ReminderDialogues(runtime.repository)
    pending = dialogs.get(conversation, scope, runtime.clock.now())
    if action is None and pending is None:
        return None
    if action == "abandon":
        if pending is None:
            return None
        dialogs.clear(conversation, scope)
        return "好的，这次提醒不设置了。"
    if action == "list":
        rows = runtime.repository.listing(5, conversation_id=conversation, source_scope=scope)
        snippets = [describe({**r, "text": r["text"][:160]}) for r in rows["items"]]
        if rows["next_cursor"] is not None:
            snippets.append(f"继续查看：/remind list {rows['next_cursor']}")
        return "\n\n".join(snippets) or "当前聊天还没有提醒记录。"
    if action in {"cancel_latest", "cancel_ambiguous"}:
        return _cancel(runtime, conversation, scope, runtime.clock.now(), action)
    if has_attachments:
        return "提醒设置暂只接受纯文本，请将图片与提醒请求分开发送。" if action else None
    if UNTRUSTED.search(text) or len(text) > 1600:
        return None
    match = CREATE.fullmatch(text.strip()) if action == "create" else None
    if match:
        before = parse_time(match["before"], received_at)
        if before.error:
            return "我没能确定这个时间。请明确一个日期和时刻，例如“明天上午九点提醒我交材料”。"
        # A quoted/discussed request or a sentence about someone else's reminder grants nothing.
        prefix = re.sub(
            r"请你|请|麻烦你|麻烦|帮我|能不能|可以|我希望你|记得|在|于|，|,|\s",
            "",
            before.remainder,
        )
        if prefix:
            return None
        fragment = match["before"] + " " + match["after"]
    else:
        if pending is None or len(text) > 120 or "\n" in text:
            return None
        if META.search(text) or re.search(r"(?:准备|打算|计划|我想|我今天|我明天)", text):
            return None
        if re.search(
            r"谢谢|算了|不用|不要|不想|改天|下次|先不|换个话题|[？?]", text
        ) or re.fullmatch(r"(?:好|好呀|好的|嗯|嗯嗯|我今天有点累)[。！!]?", text.strip()):
            return None
        fragment = text
    prefix_time = bool(match and (before.due_at is not None or before.hour is not None))
    parts = before if prefix_time else parse_time(fragment, received_at)
    if parts.error:
        return "我没能确定这个时间。请明确一个日期和时刻，例如“明天上午九点提醒我交材料”。"
    body = _body(parts.remainder)
    if not match and re.fullmatch(r"(?:那就|就)?(?:吧|就好|就行|好了|可以)?", body):
        body = ""
    if match:
        # Remove the gate phrase and polite words only on the time-prefix side.
        tail = parse_time(match["after"], received_at) if not prefix_time else None
        body = _body(tail.remainder if tail else match["after"])
        if body in {"一下", "一声", "吧", "一下吧", "一声吧", ""}:
            body = ""
    if SECRET.search(body):
        return "提醒内容可能包含凭据，请移除后再设置。"
    # An explicit new request starts fresh, except a reply supplying a previously missing body.
    reuse = pending is not None and (
        match is None or (not pending.get("text") and not before.day and before.hour is None)
    )
    draft = (
        Draft.model_validate(pending)
        if reuse
        else Draft(request_id=request_key(scope, message_id), anchor=received_at)
    )
    if match is None and draft.text and body:
        return None  # An unrelated sentence cannot silently replace a specified reminder body.
    if parts.day is not None:
        draft.day, draft.due_at = parts.day, None
    if parts.hour is not None:
        draft.hour, draft.minute = parts.hour, parts.minute
        draft.clock_24h, draft.due_at = parts.clock_24h, None
        if parts.clock_24h and parts.period is None:
            draft.period = None
    if parts.period is not None:
        draft.period, draft.due_at = parts.period, None
    if parts.due_at is not None:
        draft.due_at = parts.due_at
    if body:
        draft.text = body
    prompt = _prompt(draft)
    if prompt is not None:
        runtime._require_write()
        dialogs.save(conversation, scope, draft.model_dump(mode="json"), runtime.clock.now())
        return prompt + "（这条提醒尚未创建。）"
    try:
        result = runtime.create(
            ReminderRequest(
                request_id=draft.request_id,
                conversation_id=conversation,
                source_scope=scope,
                due_at=draft.due_at,
                text=draft.text,
            )
        )
    except ReminderError as error:
        if error.code == "reminder_time_out_of_range":
            return "这个时间已过去或超出未来一年范围。我没有顺延日期，请重新指定完整提醒。"
        raise
    dialogs.clear(conversation, scope)
    item = result["reminder"]
    if item["status"] == "blocked":
        return "这条提醒已到期，当前还未确认投递。"
    if item["status"] != "scheduled":
        return "这条提醒已在处理或已结束，没有重复设置。"
    when = _when(datetime.fromisoformat(item["due_at"]), runtime.clock.now())
    if result["delivery_available"]:
        return f"好，{when}提醒你{item['text']}。"
    return f"记下了：{when}，{item['text']}。不过当前提醒渠道不可用，暂时不能保证通知到你。"
