"""Root-relative discovery and conservative maintenance of generated Markdown entries."""

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

DIRECTORY_SUMMARY = "此目录整理按主题组织的长期知识；请查看下方索引选择相关主题。"
LINK = re.compile(r"[ \t]*- \[[^\]\r\n]+\]\((?P<target>[^)\r\n]+)\)[^\r\n]*")
MANAGED = re.compile(r" <!-- heybuddy-index:([0-9a-f]{64}) -->$")


def link_matches(text: str) -> list[re.Match[str]]:
    """Keep original character spans and line endings; skip fenced examples."""
    matches = []
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith(("```", "~~~")):
            marker = line.lstrip()[:3]
            fence = marker if fence is None else (None if fence == marker else fence)
        elif fence is None and (match := LINK.match(text, offset)) is not None:
            matches.append(match)
        offset += len(line)
    return matches


def links(text: str) -> set[str]:
    return {match["target"] for match in link_matches(text)}


def entry_text(target: str, name: str, description: str) -> str:
    label = re.sub(r"[\[\]\\]", "", name) or PurePosixPath(target).stem
    return f"- [{label}]({target}): {description}"


def managed_entry(target: str, name: str, description: str) -> str:
    visible = entry_text(target, name, description)
    return f"{visible} <!-- heybuddy-index:{digest(visible.encode('utf-8'))} -->"


def is_managed(line: str) -> bool:
    marker = MANAGED.search(line)
    return marker is not None and digest(line[: marker.start()].encode("utf-8")) == marker[1]


def index_path(directory: str) -> str:
    return f"{directory}/MEMORY.md" if directory else "MEMORY.md"


def append_link(fs: MemoryFS, directory: str, target: str, name: str, description: str) -> bool:
    relative = index_path(directory)
    raw = fs.raw(fs.path(relative), fs.max_bytes if not directory else 65536)
    text = raw.decode("utf-8")
    if target in links(text):
        return False
    addition = "\n" + managed_entry(target, name, description) + "\n"
    if target not in links(text + addition):
        raise CoreMemoryError(
            "invalid_index", "Unclosed code fence prevents appending a discoverable link."
        )
    fs.replace(relative, text + addition, expected_hash=digest(raw))
    return True


def ensure_chain(fs: MemoryFS, relative: str) -> None:
    parts = relative.split("/")
    for depth in range(1, len(parts)):
        directory = "/".join(parts[:depth])
        path = fs.path(directory, directory=True)
        path.mkdir(exist_ok=True)
        index = index_path(directory)
        if not fs.path(index).exists():
            fs.replace(index, f"# {parts[depth - 1]}\n\n{DIRECTORY_SUMMARY}\n", expected_hash=None)
        text = fs.raw(fs.path(index)).decode("utf-8")
        append_link(fs, "/".join(parts[: depth - 1]), index, parts[depth - 1], summary(text))
    raw = fs.raw(fs.path(relative))
    name, description = metadata(raw.decode("utf-8"))
    append_link(fs, "/".join(parts[:-1]), relative, name, description)


def create(fs: MemoryFS, relative: str, content: str) -> dict:
    path = fs.path(relative)
    if "/" not in relative or path.name == "MEMORY.md":
        raise CoreMemoryError("invalid_path", "Create a topic file beneath an indexed directory.")
    metadata(content)
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
            ensure_chain(fs, relative)
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


def maintain_link(
    fs: MemoryFS,
    directory: str,
    target: str,
    name: str,
    description: str,
) -> dict[str, str]:
    """Refresh only an intact generated entry; never infer ownership from prose equality."""
    relative = index_path(directory)
    path = fs.path(relative)
    if not path.exists():
        raise CoreMemoryError(
            "missing_index", "Parent index is missing; inspect and explicitly repair it."
        )
    raw = fs.raw(path, fs.max_bytes if not directory else 65536)
    text = raw.decode("utf-8")
    matches = [match for match in link_matches(text) if match["target"] == target]
    result = {"path": relative, "target": target}
    if not matches:
        append_link(fs, directory, target, name, description)
        return {**result, "status": "added"}
    if len(matches) != 1:
        return {**result, "status": "review_required", "reason": "duplicate_link"}
    match = matches[0]
    if not is_managed(match[0]):
        return {**result, "status": "review_required", "reason": "custom_or_unmanaged_link"}
    replacement = managed_entry(target, name, description)
    if match[0] == replacement:
        return {**result, "status": "unchanged"}
    updated = text[: match.start()] + replacement + text[match.end() :]
    fs.replace(relative, updated, expected_hash=digest(raw))
    return {**result, "status": "updated"}


def edit(
    fs: MemoryFS,
    relative: str,
    old_text: str,
    new_text: str,
    *,
    expected_hash: str,
) -> dict:
    """Save a local edit, then maintain discovery under the same short write lock."""
    if "/" not in relative:
        raise CoreMemoryError("invalid_path", "Use Core tools to edit root memory.")
    with fs.writing():
        original = fs.raw(fs.path(relative)).decode("utf-8")
        is_index = relative.endswith("/MEMORY.md")
        name = PurePosixPath(relative).parent.name
        try:
            before = (name, summary(original)) if is_index else metadata(original)
        except CoreMemoryError as exc:
            if exc.code != "invalid_metadata":
                raise
            before = None  # Preserve the ability to repair externally broken Frontmatter.
        value = fs._edit_locked(relative, old_text, new_text, expected_hash=expected_hash)
        updated = original.replace(old_text, new_text, 1)
        after = (name, summary(updated)) if is_index else metadata(updated)
        changed = before != after
        result = {
            "path": relative,
            "hash": value,
            "edited": True,
            "complete": True,
            "metadata_changed": changed and not is_index,
            "summary_changed": changed and is_index,
        }
        if not changed:
            return {**result, "index": {"status": "not_needed"}}
        directory = PurePosixPath(relative).parent
        if is_index:
            directory = directory.parent
        directory_name = "" if str(directory) == "." else str(directory)
        try:
            maintenance = maintain_link(fs, directory_name, relative, *after)
        except (CoreMemoryError, OSError, UnicodeError) as exc:
            reason = exc.code if isinstance(exc, CoreMemoryError) else "memory_io_error"
            maintenance = {
                "path": index_path(directory_name),
                "target": relative,
                "status": "failed",
                "reason": reason,
            }
        return {
            **result,
            "complete": maintenance["status"] not in {"failed", "review_required"},
            "index": maintenance,
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


def entry_issue(
    parent: str,
    target: str,
    matches: list[re.Match[str]],
    name: str,
    description: str,
) -> dict[str, str] | None:
    if len(matches) != 1 or MANAGED.search(matches[0][0]) is None:
        return None  # Unmarked prose may intentionally explain when to read the topic.
    line = matches[0][0]
    if not is_managed(line):
        return {
            "code": "index_entry_review",
            "severity": "review",
            "path": parent,
            "target": target,
            "reason": "modified_generated_entry",
        }
    if line != managed_entry(target, name, description):
        return {"code": "stale_managed_link", "path": parent, "target": target}
    return None


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
                entries = []
            else:
                text = fs.raw(fs.path(index), fs.max_bytes if not directory else 65536).decode(
                    "utf-8"
                )
                entries = link_matches(text)
                references = {match["target"] for match in entries}
                reference_count += len(entries)
                if reference_count > MAX_ENTRIES:
                    raise CoreMemoryError(
                        "memory_entry_limit", "Index links exceed the inspection budget."
                    )
                for target in sorted(references):
                    if sum(match["target"] == target for match in entries) > 1:
                        issues.append(
                            {"code": "duplicate_link", "path": index, "target": clip(target, 260)}
                        )
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
                        name, description = metadata(fs.raw(fs.path(file)).decode("utf-8"))
                        matching = [match for match in entries if match["target"] == file]
                        if issue := entry_issue(index, file, matching, name, description):
                            issues.append(issue)
                    except CoreMemoryError as exc:
                        issues.append({"code": exc.code, "path": file})
            for child in directories[1:]:
                if PurePosixPath(child).parent == PurePosixPath(directory or "."):
                    target = index_path(child)
                    if target not in references:
                        issues.append({"code": "missing_link", "path": index, "target": target})
                    elif target in known:
                        text = fs.raw(fs.path(target)).decode("utf-8")
                        matching = [match for match in entries if match["target"] == target]
                        if issue := entry_issue(
                            index, target, matching, PurePosixPath(child).name, summary(text)
                        ):
                            issues.append(issue)
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
    """Add missing discovery links and refresh intact generated entries; preserve custom text."""
    changed = 0
    updates = 0
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
                        f"# {PurePosixPath(directory).name}\n\n{DIRECTORY_SUMMARY}\n",
                        expected_hash=None,
                    )
                    changed += 1
                parent = PurePosixPath(directory).parent.as_posix()
                parent = "" if parent == "." else parent
                text = fs.raw(path).decode("utf-8")
                status = maintain_link(
                    fs, parent, index, PurePosixPath(directory).name, summary(text)
                )
                changed += status["status"] == "added"
                updates += status["status"] == "updated"
            for file in files:
                if file in BLOCKS or file.endswith("/MEMORY.md"):
                    continue
                name, description = metadata(fs.raw(fs.path(file)).decode("utf-8"))
                status = maintain_link(fs, str(PurePosixPath(file).parent), file, name, description)
                changed += status["status"] == "added"
                updates += status["status"] == "updated"
            report = inspect(fs)
        except (CoreMemoryError, OSError, UnicodeError):
            raise CoreMemoryError(
                "memory_index_incomplete",
                "Index repair stopped; some additions or updates may be saved. "
                "No content was removed. Inspect before any further repair.",
            ) from None
        return bounded({**report, "additions": changed, "updates": updates})
