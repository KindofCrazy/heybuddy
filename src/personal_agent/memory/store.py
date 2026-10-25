"""Immutable Markdown snapshots published with one atomic CURRENT pointer swap."""

import hashlib
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Self
from uuid import uuid4

from pydantic import ValidationError

from personal_agent.core.memory import CoreMemoryError
from personal_agent.file_lock import FileLock, FileLockError
from personal_agent.memory.models import (
    BLOCKS,
    BlockName,
    MemoryRevision,
    MemorySnapshot,
    render_memory,
)

DEFAULT_BLOCKS = {"persona.md": "# Persona\n", "human.md": "# Human\n", "MEMORY.md": "# Memory\n"}


class MemoryStore:
    def __init__(self, root: Path, *, max_bytes: int = 8192) -> None:
        if max_bytes < 512:
            raise ValueError("Core Memory limit must be at least 512 UTF-8 bytes")
        self.root = root.absolute()
        self.max_bytes = max_bytes

    @classmethod
    def for_agent(cls, root: Path, agent_id: str, *, max_bytes: int = 8192) -> Self:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", agent_id):
            raise CoreMemoryError("invalid_agent", "Invalid memory agent identifier.")
        return cls(root / agent_id, max_bytes=max_bytes)

    @staticmethod
    def _guard(path: Path) -> None:
        if path.is_symlink() or path.is_junction():
            raise CoreMemoryError("invalid_memory", "Memory paths cannot be links.")

    def _check_layout(self) -> None:
        for path in (self.root, self.root / "versions", self.root / "CURRENT"):
            self._guard(path)

    @contextmanager
    def _writing(self) -> Iterator[None]:
        try:
            self._check_layout()
            self.root.mkdir(parents=True, exist_ok=True)
            self._guard(self.root / ".write.lock")
            with FileLock(self.root / ".write.lock"):
                yield
        except FileLockError:
            raise CoreMemoryError(
                "memory_busy", "Memory is being updated; retry only after review."
            ) from None
        except OSError:
            raise CoreMemoryError(
                "memory_io_error", "Memory filesystem operation failed."
            ) from None
        except (UnicodeError, ValidationError):
            raise CoreMemoryError("invalid_memory", "Memory data cannot be validated.") from None

    def _read_bytes(self, path: Path, limit: int) -> bytes:
        self._guard(path)
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise CoreMemoryError(
                "memory_too_large", "Core Memory exceeds its configured size limit."
            )
        return data

    @staticmethod
    def _version(value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise CoreMemoryError("invalid_version", "Invalid memory version identifier.")
        return value

    def _current_version(self) -> str:
        self._check_layout()
        return self._version(self._read_bytes(self.root / "CURRENT", 64).decode("ascii").strip())

    def _revision(self, version: str) -> MemoryRevision:
        directory = self.root / "versions" / self._version(version)
        self._guard(directory)
        try:
            revision = MemoryRevision.model_validate_json(
                self._read_bytes(directory / "revision.json", 4096)
            )
        except (OSError, UnicodeError, ValidationError):
            raise CoreMemoryError(
                "invalid_memory", "Memory revision cannot be validated."
            ) from None
        if revision.version != version or set(revision.hashes) != set(BLOCKS):
            raise CoreMemoryError("invalid_memory", "Invalid memory revision metadata.")
        return revision

    def _load(self, version: str) -> MemorySnapshot:
        directory = self.root / "versions" / self._version(version)
        revision = self._revision(version)
        blocks = {}
        for block in BLOCKS:
            raw = self._read_bytes(directory / block, self.max_bytes)
            if hashlib.sha256(raw).hexdigest() != revision.hashes[block]:
                raise CoreMemoryError("invalid_memory", "Memory revision integrity check failed.")
            blocks[block] = raw.decode("utf-8")
        snapshot = MemorySnapshot(revision=revision, blocks=blocks)
        self._check_size(snapshot)
        return snapshot

    def _check_size(self, snapshot: MemorySnapshot) -> None:
        if len(render_memory(snapshot).encode("utf-8")) > self.max_bytes:
            raise CoreMemoryError(
                "memory_too_large", "Compiled Core Memory exceeds its UTF-8 byte limit."
            )

    def read(self) -> MemorySnapshot:
        try:
            return self._load(self._current_version())
        except (OSError, UnicodeError, ValidationError):
            raise CoreMemoryError("invalid_memory", "Memory cannot be read or validated.") from None

    def compile(self) -> str:
        return render_memory(self.read())

    def initialize(self) -> MemorySnapshot:
        with self._writing():
            if (self.root / "CURRENT").exists():
                return self.read()
            versions = self.root / "versions"
            versions.mkdir(exist_ok=True)
            if any(versions.iterdir()):
                raise CoreMemoryError(
                    "invalid_memory",
                    "Memory pointer is missing; existing versions will not be reset.",
                )
            return self._publish(dict(DEFAULT_BLOCKS), parent=None, action="initialize")

    @staticmethod
    def _write_file(path: Path, data: bytes) -> None:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    def _publish(
        self,
        blocks: dict[BlockName, str],
        *,
        parent: str | None,
        action: str,
        restored_from: str | None = None,
    ) -> MemorySnapshot:
        revision = MemoryRevision(
            version=uuid4().hex,
            parent=parent,
            action=action,
            restored_from=restored_from,
            created_at=datetime.now(UTC),
            hashes={
                name: hashlib.sha256(blocks[name].encode("utf-8")).hexdigest() for name in BLOCKS
            },
        )
        snapshot = MemorySnapshot(revision=revision, blocks=blocks)
        self._check_size(snapshot)
        directory = self.root / "versions" / revision.version
        directory.mkdir()
        for name in BLOCKS:
            self._write_file(directory / name, blocks[name].encode("utf-8"))
        self._write_file(directory / "revision.json", revision.model_dump_json().encode("utf-8"))
        pointer = self.root / f".CURRENT-{revision.version}.tmp"
        try:
            self._write_file(pointer, (revision.version + "\n").encode("ascii"))
            # Linearization point and final fallible step. Before this succeeds,
            # readers still see the previous entire snapshot, never a mixed set.
            os.replace(pointer, self.root / "CURRENT")
        finally:
            try:
                pointer.unlink(missing_ok=True)
            except OSError:
                pass
        # Failed pre-publication snapshots are unreachable, kept for manual diagnosis.
        return snapshot

    def update(self, block: BlockName, content: str, *, expected_version: str) -> MemorySnapshot:
        if block not in BLOCKS:
            raise CoreMemoryError(
                "invalid_block", "Only persona.md, human.md and MEMORY.md are writable."
            )
        with self._writing():
            current = self.read()
            if current.revision.version != expected_version:
                raise CoreMemoryError(
                    "memory_conflict", "Memory changed; read the current revision before editing."
                )
            blocks = dict(current.blocks)
            blocks[block] = content
            return self._publish(blocks, parent=expected_version, action="update")

    def history(self, *, limit: int = 20) -> list[MemoryRevision]:
        if not 1 <= limit <= 100:
            raise ValueError("History limit must be between 1 and 100")
        try:
            version = self._current_version()
            revisions = []
            seen = set()
            while version is not None and len(revisions) < limit:
                if version in seen:
                    raise CoreMemoryError(
                        "invalid_memory", "Memory revision chain contains a cycle."
                    )
                seen.add(version)
                revision = self._revision(version)
                revisions.append(revision)
                version = revision.parent
            return revisions
        except (OSError, UnicodeError, ValidationError):
            raise CoreMemoryError("invalid_memory", "Memory history cannot be validated.") from None

    def restore(self, version: str, *, expected_version: str) -> MemorySnapshot:
        self._version(version)
        with self._writing():
            current = self.read()
            if current.revision.version != expected_version:
                raise CoreMemoryError("memory_conflict", "Memory changed; review before restoring.")
            # Only ancestors of the committed pointer are restorable, not orphans
            # left behind by a failed publication or snapshots from another agent.
            cursor = current.revision
            seen = set()
            while cursor.version != version:
                if cursor.version in seen or cursor.parent is None:
                    raise CoreMemoryError(
                        "unknown_version", "Version is not in committed memory history."
                    )
                seen.add(cursor.version)
                cursor = self._revision(cursor.parent)
            target = self._load(version)
            return self._publish(
                dict(target.blocks),
                parent=expected_version,
                action="restore",
                restored_from=version,
            )
