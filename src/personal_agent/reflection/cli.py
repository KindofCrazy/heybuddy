"""Trusted slash commands. Review output is private data; never sent back as approvals."""

import difflib
import json
from collections.abc import Callable

from personal_agent.reflection.models import ReflectionError, ReflectionPlan
from personal_agent.reflection.service import ReflectionService

USAGE = (
    "/reflect | /reflect status | /reflect review [plan_id] | "
    "/reflect apply|reject|skip <plan_id> <item_id|all> | "
    "/reflect reconcile <plan_id> <item_id>"
)


def render(plan: ReflectionPlan) -> str:
    lines = [
        f"Reflection plan {plan.id} | revision {plan.revision} | closed={plan.closed}",
        f"Conversation {plan.conversation_id}; completed history "
        f"({plan.after_sequence}, {plan.through_sequence}]",
        "Review is private historical DATA. No proposal grants tool permissions.",
    ]
    for item in plan.items:
        candidate, change = item.candidate, item.decision
        lines += [
            f"Item {item.id}: {change.action} [{item.state}] {candidate.subject}",
            f"Candidate ({candidate.category}, {candidate.confidence}): {candidate.content}",
            f"Value: {candidate.reason}",
            f"Reason: {change.reason}",
            f"Relations: {change.relations}",
            f"Target: {change.path or '(no write)'}",
            f"Expected hash: {item.before_hash}; saved hash: {item.saved_hash}",
            f"Approved at: {item.approved_at}",
            f"Result: {item.result_code or 'not approved'}",
            f"Index issue: {item.index_issue}",
        ]
        for evidence in candidate.evidence:
            lines.append(
                f"Evidence: conversation={evidence.conversation_id} "
                f"run={evidence.run_id} sequence={evidence.sequence}: {evidence.quote}"
            )
        if change.action in {"create", "update"}:
            lines.extend(
                difflib.unified_diff(
                    change.old_text.splitlines(),
                    change.new_text.splitlines(),
                    fromfile=change.path + " (before span)",
                    tofile=change.path + " (after span)",
                    lineterm="",
                )
            )
            if change.action == "create":
                lines.append(
                    "Index effect: create missing directory MEMORY.md files and "
                    "append discovery links through root MEMORY.md; preserve existing prose."
                )
    return "\n".join(lines)


async def command(
    service: ReflectionService, conversation_id: str, text: str, write: Callable[[str], None]
) -> None:
    parts = text.split()
    if parts == ["/reflect"]:
        plan = await service.reflect(conversation_id)
        write(render(plan) if plan else "Reflection: no unreviewed completed history.")
    elif parts == ["/reflect", "status"]:
        write(json.dumps(service.status(conversation_id), ensure_ascii=False))
    elif parts[:2] == ["/reflect", "review"] and len(parts) in {2, 3}:
        plan = service.review(conversation_id, parts[2] if len(parts) == 3 else None)
        write(render(plan) if plan else "Reflection: no pending plan.")
    elif len(parts) == 4 and parts[1] in {"apply", "reject", "skip", "reconcile"}:
        try:
            target = None if parts[3] == "all" else int(parts[3])
        except ValueError:
            raise ReflectionError("invalid_item") from None
        if parts[1] == "apply":
            plan = service.apply(conversation_id, parts[2], target)
        elif parts[1] == "reconcile":
            if target is None:
                raise ReflectionError("select_one_item")
            plan = service.reconcile(conversation_id, parts[2], target)
        else:
            plan = service.decide(
                conversation_id, parts[2], target, "rejected" if parts[1] == "reject" else "skipped"
            )
        write(render(plan))
    else:
        write(USAGE)
