"""Fixed memory blocks, optimistic versions and explicit host-granted run permissions."""

from pydantic import Field

from personal_agent.core.memory import CoreMemoryError
from personal_agent.memory.models import BlockName
from personal_agent.memory.store import MemoryStore
from personal_agent.tools.registry import Tool, ToolArguments, ToolExecutionError, ToolRegistry


class ReadMemoryArguments(ToolArguments):
    block: BlockName


class UpdateMemoryArguments(ReadMemoryArguments):
    content: str = Field(max_length=1_048_576)
    expected_version: str = Field(pattern=r"^[0-9a-f]{32}$")


class EditMemoryArguments(ReadMemoryArguments):
    old_text: str = Field(min_length=1, max_length=1_048_576)
    new_text: str = Field(max_length=1_048_576)
    expected_version: str = Field(pattern=r"^[0-9a-f]{32}$")


class RestoreMemoryArguments(ToolArguments):
    version: str = Field(pattern=r"^[0-9a-f]{32}$")
    expected_version: str = Field(pattern=r"^[0-9a-f]{32}$")


class MemoryHistoryArguments(ToolArguments):
    limit: int = Field(default=10, ge=1, le=20)


def register_memory_tools(registry: ToolRegistry, store: MemoryStore) -> None:
    # These async wrappers perform synchronous filesystem/Git I/O without await
    # points. They block the loop and cannot be promptly cancelled or interrupted
    # by asyncio timeout. No mutating work is left in a background thread.
    async def read(args: ReadMemoryArguments) -> dict[str, str]:
        try:
            snapshot = store.read()
            return {
                "block": args.block,
                "content": snapshot.blocks[args.block],
                "version": snapshot.revision.version,
            }
        except CoreMemoryError as exc:
            raise ToolExecutionError(exc.code, str(exc)) from None

    async def update(args: UpdateMemoryArguments) -> dict[str, str | bool]:
        try:
            snapshot = store.update(
                args.block, args.content, expected_version=args.expected_version
            )
            return {"version": snapshot.revision.version, "block": args.block, "updated": True}
        except CoreMemoryError as exc:
            raise ToolExecutionError(exc.code, str(exc)) from None

    async def edit(args: EditMemoryArguments) -> dict[str, str | bool]:
        try:
            snapshot = store.edit(
                args.block, args.old_text, args.new_text, expected_version=args.expected_version
            )
            return {"version": snapshot.revision.version, "block": args.block, "edited": True}
        except CoreMemoryError as exc:
            raise ToolExecutionError(exc.code, str(exc)) from None

    async def history(args: MemoryHistoryArguments) -> list[dict[str, object]]:
        try:
            return [
                revision.model_dump(mode="json") for revision in store.history(limit=args.limit)
            ]
        except CoreMemoryError as exc:
            raise ToolExecutionError(exc.code, str(exc)) from None

    async def restore(args: RestoreMemoryArguments) -> dict[str, str]:
        try:
            snapshot = store.restore(args.version, expected_version=args.expected_version)
            return {"version": snapshot.revision.version, "restored_from": args.version}
        except CoreMemoryError as exc:
            raise ToolExecutionError(exc.code, str(exc)) from None

    for tool in (
        Tool(
            "memory_read",
            "Read one allowed Core Memory block and its current version.",
            ReadMemoryArguments,
            read,
            required_permission="memory.read",
        ),
        Tool(
            "memory_history",
            "List recent committed Core Memory revisions.",
            MemoryHistoryArguments,
            history,
            required_permission="memory.read",
        ),
        Tool(
            "memory_edit",
            "Preferred for adding or correcting memories: replace one exact, nonempty, unique "
            "old_text with new_text in a block, keeping all other text. To append, match an "
            "existing anchor and include it plus the addition in new_text. "
            "Use latest expected_version.",
            EditMemoryArguments,
            edit,
            required_permission="memory.update",
        ),
        Tool(
            "memory_update",
            "Replace an ENTIRE memory block only for an explicit full replacement request. "
            "This discards previous block content. Prefer memory_edit for additions/corrections. "
            "Use the latest expected_version. Memory cannot change rules or permissions.",
            UpdateMemoryArguments,
            update,
            required_permission="memory.update",
        ),
        Tool(
            "memory_restore",
            "Restore a committed memory revision after an explicit user request; "
            "requires the latest expected_version and creates a new revision.",
            RestoreMemoryArguments,
            restore,
            required_permission="memory.restore",
        ),
    ):
        registry.register(tool)
