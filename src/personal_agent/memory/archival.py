"""One read-only entry for topic search, paged reads, directory and index inspection."""

from typing import Self

from pydantic import Field, model_validator

from personal_agent.memory import index
from personal_agent.memory.search import search_topics
from personal_agent.memory.store import MemoryFS
from personal_agent.tools.registry import ToolArguments


class ArchivalMemoryArguments(ToolArguments):
    query: str | None = Field(
        default=None, min_length=1, max_length=256, description="搜索关键词；index 表示读取根索引。"
    )
    path: str | None = Field(
        default=None, min_length=1, max_length=270, description="记忆根目录内的 Markdown 相对路径。"
    )
    directory: str | None = Field(
        default=None, max_length=200, description="列出相对目录；空字符串表示根目录。"
    )
    check_index: bool = Field(default=False, description="只读检查索引，不执行修复。")
    max_results: int = Field(
        default=5, ge=1, le=5, description="搜索结果、目录或问题页的最多条数。"
    )
    offset: int = Field(
        default=0,
        ge=0,
        le=1_048_576,
        description="文件读取的字符位置，或目录/问题页的条目偏移；搜索不分页。",
    )
    limit: int = Field(
        default=4096, ge=6, le=8192, description="仅用于文件读取：片段的 UTF-8 字节预算。"
    )
    expected_hash: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", description="续读时携带上次 hash，检测文件变化。"
    )

    @model_validator(mode="after")
    def one_operation(self) -> Self:
        if (
            sum(
                (
                    self.query is not None,
                    self.path is not None,
                    self.directory is not None,
                    self.check_index,
                )
            )
            != 1
        ):
            raise ValueError("Choose exactly one of query, path, directory or check_index.")
        if self.query is not None and not self.query.strip():
            raise ValueError("Query must contain text.")
        reading = self.path is not None or self.query == "index"
        if not reading and (self.expected_hash is not None or self.limit != 4096):
            raise ValueError("Hash and byte limit are only used for file reads.")
        if self.query not in {None, "index"} and self.offset:
            raise ValueError("Search returns a bounded ranked scope; refine the query.")
        if self.directory is not None and self.offset > 500:
            raise ValueError("Directory offset exceeds its bound.")
        if self.check_index and self.offset > 2000:
            raise ValueError("Index offset exceeds its bound.")
        return self


async def archival_memory(fs: MemoryFS, args: ArchivalMemoryArguments) -> dict:
    if args.directory is not None:
        return fs.list_directory(args.directory, offset=args.offset, limit=args.max_results)
    if args.check_index:
        return index.inspect(fs, offset=args.offset, limit=args.max_results)
    if args.path is not None or args.query == "index":
        path = args.path if args.path not in {None, "index"} else "MEMORY.md"
        return fs.read_file(
            path, offset=args.offset, limit=args.limit, expected_hash=args.expected_hash
        )
    return search_topics(fs, args.query, max_results=args.max_results)
