"""Bounded MemoryFS presentation, without recursive scans, repairs or file writes."""

import os
from datetime import UTC, datetime

from markdown_it import MarkdownIt

from personal_agent.core.memory import CoreMemoryError
from personal_agent.dashboard.privacy import safe
from personal_agent.memory.models import BLOCKS, FILE_BYTES, MAX_ENTRIES, digest
from personal_agent.memory.store import MemoryFS

MARKDOWN = MarkdownIt("commonmark", {"html": False}).disable(["image", "link"])


def markdown(text: str) -> str:
    # Raw HTML is escaped; links/images are inert and never fetch remote resources.
    return MARKDOWN.render(safe(text))


class MemoryView:
    def __init__(self, memory: MemoryFS) -> None:
        self.memory = memory

    def listing(self, directory: str, limit: int, cursor: str) -> dict:
        memory = self.memory
        root = memory.path(directory, directory=True)
        entries = []
        scanned = 0
        with memory.io(), os.scandir(root) as stream:
            for entry in stream:
                scanned += 1
                if scanned > MAX_ENTRIES:
                    raise CoreMemoryError("memory_entry_limit", "Directory exceeds budget.")
                relative = f"{directory}/{entry.name}" if directory else entry.name
                try:
                    # Guard before is_dir: never follow a junction, symlink or hard link.
                    memory.guard(root / entry.name)
                    is_dir = entry.is_dir(follow_symlinks=False)
                    path = memory.path(relative, directory=is_dir)
                    if not is_dir and not path.is_file():
                        continue
                except CoreMemoryError:
                    continue
                if entry.name > cursor:
                    entries.append(
                        {
                            "id": relative,
                            "path": relative,
                            "name": entry.name,
                            "kind": "directory" if is_dir else "file",
                            "key": entry.name,
                        }
                    )
        entries.sort(key=lambda item: item["key"])
        more = len(entries) > limit
        items = entries[:limit]
        return {
            "items": items,
            "directory": directory,
            "has_more": more,
            "next_cursor": items[-1]["key"] if more else None,
        }

    def file(self, relative: str, offset: int, expected_hash: str | None) -> dict:
        memory = self.memory
        with memory.io():
            path = memory.path(relative)
            raw = memory.raw(path, memory.max_bytes if relative in BLOCKS else FILE_BYTES)
            current_hash = digest(raw)
            if expected_hash is not None and expected_hash != current_hash:
                raise CoreMemoryError("memory_conflict", "File changed; reopen the inspector.")
            text = raw.decode("utf-8")
            if offset > len(text):
                raise CoreMemoryError("invalid_range", "File offset exceeds content.")
            # Character offsets, with fixed UTF-8 response bounds (at most 16 KiB text).
            excerpt = text[offset : offset + 4096]
            end = offset + len(excerpt)
            return {
                "path": relative,
                "hash": current_hash,
                "content": safe(excerpt),
                "html": markdown(excerpt),
                "offset": offset,
                "next_offset": end if end < len(text) else None,
                "truncated": end < len(text),
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
                "history": "Operation receipts only; no complete memory version history.",
            }
