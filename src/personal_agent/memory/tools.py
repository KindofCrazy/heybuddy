"""Run-scoped writes; bounded on-demand Deferred reads use the existing registry."""

from collections.abc import Awaitable, Callable

from pydantic import Field

from personal_agent.core.memory import CoreMemoryError
from personal_agent.memory import index
from personal_agent.memory.models import BlockName, bounded
from personal_agent.memory.store import MemoryFS
from personal_agent.tools.registry import Tool, ToolArguments, ToolExecutionError, ToolRegistry


class ReadMemoryArguments(ToolArguments):
    block: BlockName


class UpdateMemoryArguments(ReadMemoryArguments):
    content: str = Field(max_length=1_048_576)
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class EditMemoryArguments(ReadMemoryArguments):
    old_text: str = Field(min_length=1, max_length=1_048_576)
    new_text: str = Field(max_length=1_048_576)
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ListArguments(ToolArguments):
    directory: str = Field(default="", max_length=200)
    offset: int = Field(default=0, ge=0, le=500)
    limit: int = Field(default=10, ge=1, le=10)


class ReadArguments(ToolArguments):
    path: str = Field(min_length=1, max_length=270)
    offset: int = Field(default=0, ge=0, le=1_048_576)
    limit: int = Field(default=4096, ge=6, le=8192)
    expected_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class CreateArguments(ToolArguments):
    path: str = Field(min_length=1, max_length=270)
    content: str = Field(min_length=1, max_length=65536)


class EditArguments(ToolArguments):
    path: str = Field(min_length=1, max_length=270)
    old_text: str = Field(min_length=1, max_length=65536)
    new_text: str = Field(max_length=65536)
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class CheckArguments(ToolArguments):
    offset: int = Field(default=0, ge=0, le=2000)
    limit: int = Field(default=20, ge=1, le=20)


class RepairArguments(ToolArguments):
    pass


def register_memory_tools(registry: ToolRegistry, fs: MemoryFS) -> None:
    # No await inside disk steps: cancellation cannot leave a mutating worker thread.
    async def read(args: ReadMemoryArguments) -> dict:
        data = fs.read()
        return {
            "trust": "data_only",
            "block": args.block,
            "content": data.blocks[args.block],
            "hash": data.hashes[args.block],
        }

    async def update(args: UpdateMemoryArguments) -> dict:
        data = fs.update(args.block, args.content, expected_hash=args.expected_hash)
        return {"block": args.block, "hash": data.hashes[args.block], "updated": True}

    async def edit(args: EditMemoryArguments) -> dict:
        value = fs.edit(args.block, args.old_text, args.new_text, expected_hash=args.expected_hash)
        return {"block": args.block, "hash": value, "edited": True}

    async def listing(args: ListArguments) -> dict:
        return fs.list_directory(args.directory, offset=args.offset, limit=args.limit)

    async def deferred_read(args: ReadArguments) -> dict:
        return fs.read_file(
            args.path, offset=args.offset, limit=args.limit, expected_hash=args.expected_hash
        )

    async def create(args: CreateArguments) -> dict:
        return index.create(fs, args.path, args.content)

    async def deferred_edit(args: EditArguments) -> dict:
        result = bounded(
            index.edit(
                fs, args.path, args.old_text, args.new_text, expected_hash=args.expected_hash
            )
        )
        if not result["complete"]:
            code = (
                "memory_index_review"
                if result["index"]["status"] == "review_required"
                else "memory_index_incomplete"
            )
            raise ToolExecutionError(
                code,
                "Topic/index edit was saved, but parent discovery maintenance is incomplete. "
                "See data.index.path/reason; do not repeat the saved edit. Review or repair the"
                " index.",
                data=result,
            )
        return result

    async def check(args: CheckArguments) -> dict:
        return index.inspect(fs, offset=args.offset, limit=args.limit)

    async def repair(args: RepairArguments) -> dict:
        return index.repair(fs)

    def safe[Args: ToolArguments](
        function: Callable[[Args], Awaitable[dict]],
    ) -> Callable[[Args], Awaitable[dict]]:
        async def execute(args: Args) -> dict:
            try:
                return bounded(await function(args))
            except CoreMemoryError as exc:
                raise ToolExecutionError(exc.code, str(exc)) from None

        return execute

    for name, description, arguments, function, permission in (
        (
            "memory_read",
            (
                "Read an authorized Core block (persona.md/human.md/MEMORY.md) with its latest "
                "hash. Core is already in context; read again before an authorized edit, not "
                "for ordinary Q&A."
            ),
            ReadMemoryArguments,
            read,
            "memory.read",
        ),
        (
            "memory_edit",
            (
                "Authorized Core local edit: read first, pass latest expected_hash and nonempty"
                " unique old_text/new_text. Preserve all other facts. Conflict means "
                "read/review, not blind retry."
            ),
            EditMemoryArguments,
            edit,
            "memory.update",
        ),
        (
            "memory_update",
            (
                "Authorized full Core block replacement only when explicitly requested. Read "
                "current block/hash first; omitted content is removed. Prefer memory_edit for "
                "local changes."
            ),
            UpdateMemoryArguments,
            update,
            "memory.update",
        ),
        (
            "deferred_list",
            (
                'List metadata, not full knowledge. directory="" means root (never "." or "/");'
                " other directories are root-relative. Follow relevant MEMORY.md links, use "
                "offset for bounded pages, avoid broad scans."
            ),
            ListArguments,
            listing,
            None,
        ),
        (
            "deferred_read",
            (
                "Read a relevant index/topic by root-relative path after following MEMORY.md. "
                "Returns content/hash, truncated and next_offset. Continue with "
                "offset/expected_hash only if needed; missing/error means acknowledge, not "
                "invent content."
            ),
            ReadArguments,
            deferred_read,
            None,
        ),
        (
            "deferred_create",
            (
                "Authorized new topic only after checking existing indexes for its subject. "
                "content needs name/description frontmatter. Host adds index links. Partial "
                "indexing means file may exist: inspect/check, never recreate blindly."
            ),
            CreateArguments,
            create,
            "deferred.update",
        ),
        (
            "deferred_edit",
            (
                "Authorized local topic/index edit: first read latest hash, then one exact "
                "unique old_text span. Preserve unrelated knowledge. data.edited=true on "
                "partial failure means saved: review data.index.path/reason rather than repeat."
                " Custom index prose is preserved."
            ),
            EditArguments,
            deferred_edit,
            "deferred.update",
        ),
        (
            "deferred_index_check",
            (
                "Read-only bounded index audit for missing/dangling/invalid/duplicate links and"
                " stale generated metadata. Paginate issues; custom prose need not match "
                "frontmatter. Use after discovery/index trouble, not ordinary questions."
            ),
            CheckArguments,
            check,
            None,
        ),
        (
            "deferred_index_repair",
            (
                "Authorized repair adds missing discovery links/indexes and refreshes intact "
                "generated entries only. Preserves manual/unmarked prose; unresolved issues "
                "yield data.ok=false. Review leftovers, no automatic rollback/retry."
            ),
            RepairArguments,
            repair,
            "deferred.update",
        ),
    ):
        registry.register(
            Tool(name, description, arguments, safe(function), required_permission=permission)
        )
