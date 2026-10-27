"""One restricted writer for human-approved Reflection and host-controlled learning."""

from personal_agent.core.memory import CoreMemoryError
from personal_agent.memory import index
from personal_agent.memory.models import BLOCKS, FILE_BYTES, digest
from personal_agent.memory.store import MemoryFS
from personal_agent.reflection.memory import indexed, maintenance_resolved
from personal_agent.reflection.models import PlanItem


def write_item(memory: MemoryFS, item: PlanItem) -> None:
    change = item.decision
    try:
        if change.action == "create":
            result = index.create(memory, change.path, change.new_text)
        elif change.path in BLOCKS:
            saved = memory.edit(
                change.path, change.old_text, change.new_text, expected_hash=item.before_hash
            )
            result = {"hash": saved, "complete": True}
        else:
            result = index.edit(
                memory,
                change.path,
                change.old_text,
                change.new_text,
                expected_hash=item.before_hash,
            )
        item.saved_hash = result["hash"]
        item.state = "applied" if result.get("complete", True) else "partial"
        item.result_code = "saved" if item.state == "applied" else "index_incomplete"
        if item.state == "partial":
            item.index_issue = result.get("index", {})
    except CoreMemoryError as exc:
        item.state = "partial" if exc.code == "memory_index_incomplete" else "conflict"
        item.result_code = exc.code
        if item.state == "partial":
            item.saved_hash = item.after_hash


def reconcile_item(memory: MemoryFS, item: PlanItem) -> None:
    """Read only: hashes/indexes prove publication, never repeat a write."""
    change = item.decision
    try:
        with memory.io():
            raw = memory.raw(
                memory.path(change.path), memory.max_bytes if change.path in BLOCKS else FILE_BYTES
            )
            matches = digest(raw) == item.after_hash
            complete = matches and (
                change.path in BLOCKS
                or (indexed(memory, change.path) and maintenance_resolved(memory, item.index_issue))
            )
        if matches:
            item.saved_hash = item.after_hash
            item.state = "applied" if complete else "partial"
            item.result_code = "verified_saved" if complete else "index_incomplete"
        else:
            item.state = "uncertain"
            item.result_code = "saved_content_not_verified"
    except CoreMemoryError as exc:
        item.state = "uncertain"
        item.result_code = exc.code
