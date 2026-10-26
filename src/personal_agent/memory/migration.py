"""One-time V0.4 export. Legacy validation lives here, never in normal reads/writes."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from personal_agent.core.memory import CoreMemoryError
from personal_agent.memory.models import BLOCKS, CoreMemoryData, digest

if TYPE_CHECKING:
    from personal_agent.memory.store import MemoryFS

RECEIPT = ".migration-v05"


def legacy_blocks(fs: MemoryFS) -> dict[str, bytes]:
    version = fs.raw(fs.root / "CURRENT", 64).decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", version):
        raise CoreMemoryError("invalid_legacy_memory", "Invalid legacy CURRENT pointer.")
    directory = fs.root / "versions" / version
    try:
        revision = json.loads(fs.raw(directory / "revision.json", 4096))
        if revision["version"] != version or set(revision["hashes"]) != set(BLOCKS):
            raise ValueError
        raws = {name: fs.raw(directory / name, 1_048_576) for name in BLOCKS}
        if any(digest(raw) != revision["hashes"][name] for name, raw in raws.items()):
            raise ValueError
        for raw in raws.values():
            raw.decode("utf-8")
        return raws
    except (ValueError, KeyError, TypeError):
        raise CoreMemoryError(
            "invalid_legacy_memory", "Legacy memory integrity check failed."
        ) from None


def backup_legacy(fs: MemoryFS) -> str:
    # Copy everything, including unreachable snapshots, without deleting the source.
    source = fs.root / "versions"
    fs.guard(source)
    for directory, directories, files in os.walk(source, followlinks=False):
        for name in (*directories, *files):
            fs.guard(Path(directory) / name)
    name = f".migration-backup-v04-{uuid4().hex}"
    destination = fs.root / name
    destination.mkdir()
    shutil.copy2(fs.root / "CURRENT", destination / "CURRENT")
    shutil.copytree(source, destination / "versions")
    fs.atomic(destination / ".backup-complete", b"V0.4 migration backup\n", expected_hash=None)
    return name


def initialize_files(fs: MemoryFS) -> None:
    from personal_agent.memory.store import DEFAULT_BLOCKS

    fs.guard(fs.root / "CURRENT")
    fs.guard(fs.root / "versions")
    receipt_path = fs.root / RECEIPT
    fs.guard(receipt_path)
    receipt = None
    if receipt_path.exists():
        try:
            receipt = json.loads(fs.raw(receipt_path, 2048))
            if (
                not isinstance(receipt, dict)
                or set(receipt) != {"backup", "complete"}
                or type(receipt["complete"]) is not bool
                or not re.fullmatch(r"\.migration-backup-v04-[0-9a-f]{32}", receipt["backup"])
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise CoreMemoryError("invalid_migration", "Migration receipt is invalid.") from None
        if receipt["complete"]:
            return  # Only direct Markdown is authoritative after completion.
        backup = fs.root / receipt["backup"]
        fs.raw(backup / ".backup-complete", 64)
        # The backup is the pinned source when resuming an interrupted migration.
        from personal_agent.memory.store import MemoryFS

        source = MemoryFS(backup, max_bytes=fs.max_bytes)
        raws = legacy_blocks(source)
    elif (fs.root / "CURRENT").exists():
        raws = legacy_blocks(fs)
    elif (fs.root / "versions").exists():
        raise CoreMemoryError(
            "invalid_legacy_memory", "Legacy versions exist without CURRENT; will not reset."
        )
    else:
        existing = [fs.path(name).exists() for name in BLOCKS]
        if all(existing):
            return
        if any(existing):
            raise CoreMemoryError(
                "incomplete_memory", "Core files are missing; restore from your backup."
            )
        raws = {name: text.encode("utf-8") for name, text in DEFAULT_BLOCKS.items()}
        fs.check_core(
            CoreMemoryData(
                blocks={name: raw.decode("utf-8") for name, raw in raws.items()},
                hashes={name: digest(raw) for name, raw in raws.items()},
            )
        )
        for name, raw in raws.items():
            fs.atomic(fs.path(name), raw, expected_hash=None)
        return

    # Check every destination before writing any; never replace an editor's text.
    for name, raw in raws.items():
        path = fs.path(name)
        if path.exists() and fs.raw(path, 1_048_576) != raw:
            raise CoreMemoryError(
                "migration_conflict", "New Core files differ; migration will not overwrite edits."
            )
    fs.check_core(
        CoreMemoryData(
            blocks={name: raw.decode("utf-8") for name, raw in raws.items()},
            hashes={name: digest(raw) for name, raw in raws.items()},
        )
    )
    if receipt is None:
        receipt = {"backup": backup_legacy(fs), "complete": False}
        fs.atomic(receipt_path, json.dumps(receipt).encode(), expected_hash=None)
    for name, raw in raws.items():
        path = fs.path(name)
        if not path.exists():
            fs.atomic(path, raw, expected_hash=None)
        elif fs.raw(path, 1_048_576) != raw:
            raise CoreMemoryError(
                "migration_conflict", "Core file changed during migration; stopped."
            )
    expected = digest(fs.raw(receipt_path, 2048))
    receipt["complete"] = True
    fs.atomic(receipt_path, json.dumps(receipt).encode(), expected_hash=expected)
