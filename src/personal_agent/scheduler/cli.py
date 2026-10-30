"""Trusted explicit commands; no model interpretation or implicit reminder grants."""

import re
from datetime import datetime, timedelta
from uuid import uuid4

from pydantic import ValidationError

from personal_agent.proactive.delivery import FakeSender
from personal_agent.scheduler.models import ReminderError, ReminderGrant, ReminderRequest
from personal_agent.scheduler.runtime import SchedulerRuntime

USAGE = (
    "/remind at <ISO8601 with timezone> <text> | in <10m/2h/1d> <text> | "
    "status | list [cursor] | show <id> | events <id> [after] | cancel <id> | simulate <id>"
)
DURATION = re.compile(r"([1-9][0-9]{0,5})(s|m|h|d)\Z")


async def command(runtime: SchedulerRuntime, conversation_id: str, text: str) -> dict:
    parts = text.split(maxsplit=2)
    if parts == ["status"]:
        return runtime.status()
    try:
        if parts and parts[0] == "list" and len(parts) <= 2:
            return runtime.repository.listing(
                cursor=int(parts[1]) if len(parts) == 2 else 2**63 - 1
            )
        if len(parts) == 2 and parts[0] == "show":
            return runtime.repository.get(parts[1])
        if len(parts) in {2, 3} and parts[0] == "events":
            return dict(
                items=runtime.repository.events(parts[1], int(parts[2]) if len(parts) == 3 else 0)
            )
        if len(parts) == 2 and parts[0] == "cancel":
            return runtime.cancel(parts[1])
        if len(parts) == 2 and parts[0] == "simulate":
            item = runtime.repository.get(parts[1])
            # This local fake is the only Sender wired by the application.
            grant = ReminderGrant(
                enabled=True,
                conversation_id=item["conversation_id"],
                recipient="simulation:" + item["conversation_id"],
            )
            return await runtime.simulate(parts[1], FakeSender(), lambda: grant)
        if len(parts) != 3 or parts[0] not in {"at", "in"}:
            return dict(usage=USAGE)
        if parts[0] == "at":
            due_at = datetime.fromisoformat(parts[1])
        else:
            duration = DURATION.fullmatch(parts[1])
            if duration is None:
                raise ReminderError("reminder_invalid_duration")
            seconds = int(duration[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[duration[2]]
            due_at = runtime.clock.now() + timedelta(seconds=seconds)
        request = ReminderRequest(
            request_id=str(uuid4()), conversation_id=conversation_id, due_at=due_at, text=parts[2]
        )
        return runtime.create(request)
    except (ValueError, OverflowError, ValidationError):
        raise ReminderError("reminder_invalid_arguments") from None
