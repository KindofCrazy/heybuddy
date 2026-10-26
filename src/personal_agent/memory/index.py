"""Root-relative Markdown links and explicit, append-only discovery repair."""

import re
from pathlib import PurePosixPath

from personal_agent.core.memory import CoreMemoryError
from personal_agent.memory.models import (
    BLOCKS,
    MAX_ENTRIES,
    bounded,
    clip,
    digest,
    metadata,
    summary,
)
from personal_agent.memory.store import MemoryFS

LINK = re.compile(r"^\s*- \[[^\]\n]+\]\(([^)\n]+)\)", re.MULTILINE)


def links(text: str) -> set[str]:
    # Our index syntax is a bullet link outside fenced code; destinations are root-relative.
    visible = []
    fence = None
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            marker = line.lstrip()[:3]
            fence = marker if fence is None else (None if fence == marker else fence)
        elif fence is None:
            visible.append(line)
    return set(LINK.findall("\n".join(visible)))


def index_path(directory: str) -> str:
    return f"{directory}/MEMORY.md" if directory else "MEMORY.md"


def append_link(fs: MemoryFS, directory: str, target: str, name: str, description: str) -> bool:
    relative = index_path(directory)
    raw = fs.raw(fs.path(relative), fs.max_bytes if not directory else 65536)
    text = raw.decode("utf-8")
    if target in links(text):
        return False
    label = re.sub(r"[\[\]\\]", "", name)
    addition = f"\n- [{label}]({target}): {description}\n"
    if target not in links(text + addition):
        raise CoreMemoryError(
            "invalid_index", "Unclosed code fence prevents appending a discoverable link."
        )
    fs.replace(relative, text + addition, expected_hash=digest(raw))
    return True


def ensure_chain(fs: MemoryFS, relative: str, description: str) -> None:
    parts = relative.split("/")
    for depth in range(1, len(parts)):
        directory = "/".join(parts[:depth])
        path = fs.path(directory, directory=True)
        path.mkdir(exist_ok=True)
        index = index_path(directory)
        if not fs.path(index).exists():
            fs.replace(index, f"# {parts[depth - 1]}\n\n{description}\n", expected_hash=None)
        text = fs.raw(fs.path(index)).decode("utf-8")
        append_link(fs, "/".join(parts[: depth - 1]), index, parts[depth - 1], summary(text))
    raw = fs.raw(fs.path(relative))
    name, description = metadata(raw.decode("utf-8"))
    append_link(fs, "/".join(parts[:-1]), relative, name, description)


def create(fs: MemoryFS, relative: str, content: str) -> dict:
    path = fs.path(relative)
    if "/" not in relative or path.name == "MEMORY.md":
        raise CoreMemoryError("invalid_path", "Create a topic file beneath an indexed directory.")
    _, description = metadata(content)
    if len(content.encode("utf-8")) > 65536:
        raise CoreMemoryError("memory_too_large", "Knowledge file exceeds its byte limit.")
    with fs.writing():
        if path.exists():
            raise CoreMemoryError(
                "memory_conflict", "File exists; read and edit the existing topic."
            )
        # Bound total traversal before adding another controlled entry.
        directories, files = inventory(fs)
        missing_directories = sum(
            not fs.path("/".join(relative.split("/")[:depth]), directory=True).exists()
            for depth in range(1, len(relative.split("/")))
        )
        missing_indexes = sum(
            not fs.path(index_path("/".join(relative.split("/")[:depth]))).exists()
            for depth in range(1, len(relative.split("/")))
        )
        if len(directories) + len(files) + 1 + missing_directories + missing_indexes > MAX_ENTRIES:
            raise CoreMemoryError(
                "memory_entry_limit", "Creation would exceed the inspection budget."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        fs.replace(relative, content, expected_hash=None)
        try:
            ensure_chain(fs, relative, description)
        except (CoreMemoryError, OSError, UnicodeError):
            raise CoreMemoryError(
                "memory_index_incomplete",
                "Knowledge file was created, but indexing failed. Do not recreate it; "
                "use deferred_index_check then authorized "
                "deferred_index_repair. No rollback occurred.",
            ) from None
        return {
            "path": relative,
            "hash": digest(content.encode("utf-8")),
            "created": True,
            "indexed": True,
        }


def inventory(fs: MemoryFS) -> tuple[list[str], list[str]]:
    directories = [""]
    files = []
    for directory in directories:
        for path in fs.children(directory):
            relative = path.relative_to(fs.root).as_posix()
            if len(directories) + len(files) >= MAX_ENTRIES:
                raise CoreMemoryError(
                    "memory_entry_limit", "Memory tree exceeds its inspection budget."
                )
            if path.is_dir():
                fs.path(relative, directory=True)
                directories.append(relative)
            else:
                fs.path(relative)
                files.append(relative)
    return directories, files


def inspect(fs: MemoryFS, *, offset: int = 0, limit: int = 20) -> dict:
    if not 0 <= offset <= MAX_ENTRIES * 4 or not 1 <= limit <= 20:
        raise CoreMemoryError("invalid_range", "Invalid index issue page.")
    with fs.io():
        directories, files = inventory(fs)
        known = set(files)
        issues = []
        reference_count = 0
        for directory in directories:
            index = index_path(directory)
            if index not in known:
                issues.append({"code": "missing_index", "path": index})
                references = set()
            else:
                text = fs.raw(fs.path(index), fs.max_bytes if not directory else 65536).decode(
                    "utf-8"
                )
                references = links(text)
                reference_count += len(references)
                if reference_count > MAX_ENTRIES:
                    raise CoreMemoryError(
                        "memory_entry_limit", "Index links exceed the inspection budget."
                    )
                for target in sorted(references):
                    try:
                        fs.path(target)
                        parent = PurePosixPath(target).parent.as_posix()
                        parent = "" if parent == "." else parent
                        # A child directory is represented by its MEMORY.md.
                        target_directory = PurePosixPath(target).parent
                        direct = parent == directory or (
                            target.endswith("/MEMORY.md")
                            and target_directory.parent == PurePosixPath(directory or ".")
                        )
                        if not direct:
                            raise CoreMemoryError("invalid_link", "Link skips a discovery level.")
                    except CoreMemoryError:
                        issues.append(
                            {"code": "invalid_link", "path": index, "target": clip(target, 260)}
                        )
                        continue
                    if target not in known:
                        issues.append({"code": "dangling_link", "path": index, "target": target})
            for file in files:
                if file in BLOCKS or file.endswith("/MEMORY.md"):
                    continue
                if str(PurePosixPath(file).parent) == directory:
                    if file not in references:
                        issues.append({"code": "missing_link", "path": index, "target": file})
                    try:
                        metadata(fs.raw(fs.path(file)).decode("utf-8"))
                    except CoreMemoryError as exc:
                        issues.append({"code": exc.code, "path": file})
            for child in directories[1:]:
                if PurePosixPath(child).parent == PurePosixPath(directory or "."):
                    target = index_path(child)
                    if target not in references:
                        issues.append({"code": "missing_link", "path": index, "target": target})
        end = offset + limit
        return bounded(
            {
                "trust": "data_only",
                "ok": not issues,
                "total_issues": len(issues),
                "issues": issues[offset:end],
                "has_more": end < len(issues),
                "next_offset": end if end < len(issues) else None,
            }
        )


def repair(fs: MemoryFS) -> dict:
    """Only add missing indexes/links; dangling or malformed references need human review."""
    changed = 0
    with fs.writing():
        directories, files = inventory(fs)
        missing = sum(not fs.path(index_path(directory)).exists() for directory in directories)
        if len(directories) + len(files) + missing > MAX_ENTRIES:
            raise CoreMemoryError(
                "memory_entry_limit", "Repair would exceed the inspection budget."
            )
        try:
            for directory in directories[1:]:
                index = index_path(directory)
                path = fs.path(index)
                if not path.exists():
                    fs.replace(
                        index,
                        f"# {PurePosixPath(directory).name}\n\n长期主题知识。\n",
                        expected_hash=None,
                    )
                    changed += 1
                parent = PurePosixPath(directory).parent.as_posix()
                parent = "" if parent == "." else parent
                text = fs.raw(path).decode("utf-8")
                changed += append_link(
                    fs, parent, index, PurePosixPath(directory).name, summary(text)
                )
            for file in files:
                if file in BLOCKS or file.endswith("/MEMORY.md"):
                    continue
                name, description = metadata(fs.raw(fs.path(file)).decode("utf-8"))
                changed += append_link(fs, str(PurePosixPath(file).parent), file, name, description)
        except (CoreMemoryError, OSError, UnicodeError):
            raise CoreMemoryError(
                "memory_index_incomplete",
                "Index repair stopped; some additions may be saved. "
                "No content was removed. Inspect before any further repair.",
            ) from None
        return bounded({**inspect(fs), "additions": changed})
