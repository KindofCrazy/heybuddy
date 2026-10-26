"""Agent-scoped editable Markdown with short locks and atomic single-file writes."""

import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Self
from uuid import uuid4

from personal_agent.core.memory import CoreMemoryError
from personal_agent.file_lock import FileLock, FileLockError
from personal_agent.memory.models import (
    BLOCKS,
    FILE_BYTES,
    MAX_DEPTH,
    MAX_ENTRIES,
    BlockName,
    CoreMemoryData,
    bounded,
    clip,
    digest,
    metadata,
    render_memory,
    summary,
)
from personal_agent.memory.privacy import require_private_directory

DEFAULT_BLOCKS = {"persona.md": "# Persona\n", "human.md": "# Human\n", "MEMORY.md": "# Memory\n"}
SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(10)),
    *(f"lpt{i}" for i in range(10)),
}


class MemoryFS:
    def __init__(self, root: Path, *, max_bytes: int = 8192) -> None:
        if not 512 <= max_bytes <= 1_048_576:
            raise ValueError("Core Memory limit must be between 512 and 1048576 bytes")
        self.root = root.absolute()
        self.max_bytes = max_bytes

    @classmethod
    def for_agent(cls, root: Path, agent_id: str, *, max_bytes: int = 8192) -> Self:
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", agent_id) or agent_id.lower() in RESERVED:
            raise CoreMemoryError(
                "invalid_agent", "Memory agent identifiers must use lowercase portable names."
            )
        return cls(root / agent_id, max_bytes=max_bytes)

    @staticmethod
    def guard(path: Path) -> None:
        # Resolve alone hides junctions. Check all ancestors, even above Memory Root.
        for part in (path, *path.parents):
            if part.is_symlink() or part.is_junction():
                raise CoreMemoryError("invalid_path", "Memory paths cannot contain links.")
            if part.exists():
                info = part.stat(follow_symlinks=False)
                if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise CoreMemoryError(
                        "invalid_path", "Memory paths cannot contain reparse points."
                    )
                if part.is_file() and info.st_nlink > 1:
                    raise CoreMemoryError("invalid_path", "Memory files cannot be hard links.")

    def path(self, relative: str, *, directory: bool = False) -> Path:
        if directory and relative == "":
            self.guard(self.root)
            return self.root
        parts = relative.split("/")
        names = parts if directory else [*parts[:-1], parts[-1].removesuffix(".md")]
        directories = parts if directory else parts[:-1]
        if (
            len(parts) - (not directory) > MAX_DEPTH
            or any(not SEGMENT.fullmatch(name) or name.lower() in RESERVED for name in names)
            or any(name.lower() in {"versions", "skills"} for name in directories)
            or (not directory and not relative.endswith(".md"))
            or (not directory and len(parts) == 1 and relative not in BLOCKS)
            or (not directory and parts[-1].lower() == "memory.md" and parts[-1] != "MEMORY.md")
        ):
            raise CoreMemoryError(
                "invalid_path", "Use bounded root-relative Markdown paths with /."
            )
        result = self.root.joinpath(*parts)
        self.guard(result)
        if not result.resolve().is_relative_to(self.root.resolve()):
            raise CoreMemoryError("invalid_path", "Path is outside the agent memory root.")
        return result

    @contextmanager
    def io(self) -> Iterator[None]:
        try:
            yield
        except FileLockError:
            raise CoreMemoryError(
                "memory_busy", "Memory is being updated; review before retrying."
            ) from None
        except (OSError, UnicodeError):
            raise CoreMemoryError(
                "memory_io_error", "Memory filesystem operation failed."
            ) from None

    @contextmanager
    def writing(self) -> Iterator[None]:
        with self.io():
            self.guard(self.root / ".write.lock")
            require_private_directory(self.root)
            self.root.mkdir(parents=True, exist_ok=True)
            with FileLock(self.root / ".write.lock"):
                yield

    def raw(self, path: Path, limit: int = FILE_BYTES) -> bytes:
        self.guard(path)
        if not stat.S_ISREG(path.stat().st_mode):
            raise CoreMemoryError("invalid_path", "Memory reads require regular files.")
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise CoreMemoryError("memory_too_large", "Memory file exceeds its byte limit.")
        return raw

    def atomic(self, path: Path, raw: bytes, *, expected_hash: str | None) -> None:
        """Caller holds writing(). None means create-only; recheck just before replace."""
        self.guard(path)
        temporary = path.with_name(f".{path.name}-{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            self.guard(path)
            if expected_hash is None:
                if path.exists():
                    raise CoreMemoryError(
                        "memory_conflict", "File already exists; read before editing."
                    )
            elif (
                not path.exists()
                or digest(self.raw(path, max(FILE_BYTES, self.max_bytes))) != expected_hash
            ):
                raise CoreMemoryError("memory_conflict", "Memory changed; read before editing.")
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # A leftover hidden temporary file is never a memory source.

    def initialize(self) -> CoreMemoryData:
        from personal_agent.memory.migration import initialize_files

        with self.writing():
            initialize_files(self)
            return self.read()

    def read(self) -> CoreMemoryData:
        with self.io():
            raws = {name: self.raw(self.path(name), self.max_bytes) for name in BLOCKS}
            data = CoreMemoryData(
                blocks={name: raw.decode("utf-8") for name, raw in raws.items()},
                hashes={name: digest(raw) for name, raw in raws.items()},
            )
            self.check_core(data)
            return data

    def check_core(self, data: CoreMemoryData) -> None:
        if len(render_memory(data).encode("utf-8")) > self.max_bytes:
            raise CoreMemoryError(
                "memory_too_large", "Compiled Core Memory exceeds its UTF-8 byte limit."
            )

    def compile(self) -> str:
        return render_memory(self.read())

    def replace(self, relative: str, text: str, *, expected_hash: str | None) -> str:
        path = self.path(relative)
        raw = text.encode("utf-8")
        if len(raw) > (self.max_bytes if relative in BLOCKS else FILE_BYTES):
            raise CoreMemoryError("memory_too_large", "Memory file exceeds its byte limit.")
        if relative in BLOCKS:
            data = self.read()
            data.blocks[relative] = text
            data.hashes[relative] = digest(raw)
            self.check_core(data)
        elif path.name != "MEMORY.md":
            metadata(text)
        self.atomic(path, raw, expected_hash=expected_hash)
        return digest(raw)

    def update(self, block: BlockName, content: str, *, expected_hash: str) -> CoreMemoryData:
        if block not in BLOCKS:
            raise CoreMemoryError("invalid_block", "Only fixed Core Memory blocks are writable.")
        with self.writing():
            self.replace(block, content, expected_hash=expected_hash)
            return self.read()

    def edit(self, relative: str, old_text: str, new_text: str, *, expected_hash: str) -> str:
        with self.writing():
            return self._edit_locked(relative, old_text, new_text, expected_hash=expected_hash)

    def _edit_locked(
        self, relative: str, old_text: str, new_text: str, *, expected_hash: str
    ) -> str:
        """Caller holds writing(); share the lock with associated index maintenance."""
        path = self.path(relative)
        raw = self.raw(path, self.max_bytes if relative in BLOCKS else FILE_BYTES)
        if digest(raw) != expected_hash:
            raise CoreMemoryError("memory_conflict", "Memory changed; read before editing.")
        original = raw.decode("utf-8")
        start = original.find(old_text)
        if not old_text or start < 0 or original.find(old_text, start + 1) >= 0:
            raise CoreMemoryError("memory_edit_mismatch", "Original text must match exactly once.")
        text = original[:start] + new_text + original[start + len(old_text) :]
        return self.replace(relative, text, expected_hash=expected_hash)

    def read_file(
        self, relative: str, *, offset: int = 0, limit: int = 4096, expected_hash: str | None = None
    ) -> dict:
        if relative in {"persona.md", "human.md"}:
            raise CoreMemoryError("invalid_path", "Use memory_read for fixed Core blocks.")
        if not 0 <= offset <= max(FILE_BYTES, self.max_bytes) or not 6 <= limit <= 8192:
            raise CoreMemoryError("invalid_range", "Invalid character offset or byte budget.")
        with self.io():
            raw = self.raw(
                self.path(relative), self.max_bytes if relative == "MEMORY.md" else FILE_BYTES
            )
            current_hash = digest(raw)
            if expected_hash is not None and expected_hash != current_hash:
                raise CoreMemoryError("memory_conflict", "Memory changed between read fragments.")
            text = raw.decode("utf-8")
            if offset > len(text):
                raise CoreMemoryError("invalid_range", "Offset is past the end of the file.")
            content = clip(text[offset:], limit)
            end = offset + len(content)
            return bounded(
                {
                    "trust": "data_only",
                    "path": relative,
                    "hash": current_hash,
                    "content": content,
                    "offset": offset,
                    "truncated": end < len(text),
                    "next_offset": end if end < len(text) else None,
                    "total_characters": len(text),
                }
            )

    def children(self, directory: str) -> list[Path]:
        path = self.path(directory, directory=True)
        entries = []
        with os.scandir(path) as stream:
            for entry in stream:
                if entry.name.startswith(".") or (
                    directory == "" and entry.name in {"CURRENT", "versions"}
                ):
                    continue
                if len(entries) >= MAX_ENTRIES:
                    raise CoreMemoryError(
                        "memory_entry_limit", "Directory exceeds its entry budget."
                    )
                self.guard(Path(entry.path))
                entries.append(Path(entry.path))
        return sorted(entries, key=lambda item: item.name)

    def list_directory(self, directory: str = "", *, offset: int = 0, limit: int = 10) -> dict:
        if not 0 <= offset <= MAX_ENTRIES or not 1 <= limit <= 10:
            raise CoreMemoryError("invalid_range", "Invalid directory page.")
        with self.io():
            children = self.children(directory)
            entries = []
            for path in children[offset : offset + limit]:
                relative = path.relative_to(self.root).as_posix()
                if path.is_dir():
                    self.path(relative, directory=True)
                    index = self.path(relative + "/MEMORY.md")
                    text = self.raw(index).decode("utf-8") if index.exists() else ""
                    entries.append(
                        {
                            "path": relative,
                            "kind": "directory",
                            "name": path.name,
                            "description": clip(summary(text), 512),
                            "index_missing": not index.exists(),
                        }
                    )
                else:
                    self.path(relative)
                    raw = self.raw(path, self.max_bytes if relative in BLOCKS else FILE_BYTES)
                    name, description = (
                        (path.stem, summary(raw.decode("utf-8")))
                        if path.name == "MEMORY.md" or relative in BLOCKS
                        else metadata(raw.decode("utf-8"))
                    )
                    entries.append(
                        {
                            "path": relative,
                            "kind": "file",
                            "name": clip(name, 240),
                            "description": clip(description, 512),
                            "metadata_truncated": name != clip(name, 240)
                            or description != clip(description, 512),
                            "hash": digest(raw),
                        }
                    )
            index_path = self.path(f"{directory}/MEMORY.md" if directory else "MEMORY.md")
            index_summary = (
                summary(
                    self.raw(index_path, self.max_bytes if not directory else FILE_BYTES).decode(
                        "utf-8"
                    )
                )
                if index_path.exists()
                else ""
            )
            end = offset + limit
            return bounded(
                {
                    "trust": "data_only",
                    "directory": directory,
                    "summary": clip(index_summary, 512),
                    "entries": entries,
                    "has_more": end < len(children),
                    "next_offset": end if end < len(children) else None,
                }
            )
