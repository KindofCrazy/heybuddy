"""Read-only comparison views and validation of exact-span MemoryFS proposals."""

from pathlib import PurePosixPath

from personal_agent.core.evidence import redact
from personal_agent.memory import index
from personal_agent.memory.models import BLOCKS, FILE_BYTES, digest, metadata, summary
from personal_agent.memory.safety import contains_credentials
from personal_agent.memory.store import MemoryFS
from personal_agent.reflection.models import Candidate, Decision, PlanItem, ReflectionError


def catalogue(fs: MemoryFS) -> list[dict]:
    with fs.io():
        _, files = index.inventory(fs)
        result = []
        for relative in files:
            if relative in BLOCKS:
                continue
            raw = fs.raw(fs.path(relative))
            text = raw.decode("utf-8")
            name, description = (
                (PurePosixPath(relative).parent.name, summary(text))
                if relative.endswith("/MEMORY.md")
                else metadata(text)
            )
            result.append(
                {"path": relative, "name": redact(name), "description": redact(description)}
            )
        return result


def read_comparison(fs: MemoryFS, paths: list[str], known: set[str]) -> dict[str, str]:
    if len(set(paths)) != len(paths) or any(path not in known for path in paths):
        raise ReflectionError("invalid_memory_selection")
    with fs.io():
        result = dict(fs.read().blocks)
        for path in paths:
            result[path] = fs.raw(fs.path(path), FILE_BYTES).decode("utf-8")
        return result


def plan_item(
    fs: MemoryFS,
    item_id: int,
    candidate: Candidate | None,
    decision: Decision,
    comparison: dict[str, str],
    catalog: list[dict],
) -> PlanItem:
    item = PlanItem(id=item_id, candidate=candidate, decision=decision.model_copy(deep=True))
    change = item.decision
    if (
        candidate is not None
        and candidate.confidence == "uncertain"
        and change.action in {"create", "update"}
    ):
        change.action = "review"
        change.reason = "Uncertain evidence requires a new confirmed source before writing."
        change.path = change.old_text = change.new_text = ""
    if change.action not in {"create", "update"}:
        if change.path or change.old_text or change.new_text:
            raise ReflectionError("non_write_payload")
        return item
    if contains_credentials(change.new_text):
        raise ReflectionError("credentials_forbidden")
    fs.path(change.path)
    if change.action == "create":
        if "/" not in change.path or change.path.endswith("/MEMORY.md") or change.old_text:
            raise ReflectionError("invalid_create")
        name, _ = metadata(change.new_text)
        if fs.path(change.path).exists():
            raise ReflectionError("create_target_exists")
        # Exact topic collisions need a human decision; semantic similarity is model judgment.
        if any(
            row["name"].casefold() == name.casefold()
            or PurePosixPath(row["path"]).stem.casefold()
            == PurePosixPath(change.path).stem.casefold()
            for row in catalog
        ):
            change.action = "review"
            change.reason = "Existing topic may duplicate this proposal; select and update it."
            change.path = change.old_text = change.new_text = ""
            return item
        result = change.new_text
    else:
        if change.path not in comparison:
            raise ReflectionError("unread_update_target")
        before = comparison[change.path]
        start = before.find(change.old_text)
        if not change.old_text or start < 0 or before.find(change.old_text, start + 1) >= 0:
            raise ReflectionError("edit_not_unique")
        result = before[:start] + change.new_text + before[start + len(change.old_text) :]
        item.before_hash = digest(before.encode())
        if change.path in BLOCKS:
            data = fs.read()
            if data.hashes[change.path] != item.before_hash:
                raise ReflectionError("memory_changed_during_plan")
            data.blocks[change.path] = result
            fs.check_core(data)
        elif not change.path.endswith("/MEMORY.md"):
            metadata(result)
    limit = fs.max_bytes if change.path in BLOCKS else FILE_BYTES
    if len(result.encode()) > limit:
        raise ReflectionError("memory_too_large")
    item.after_hash = digest(result.encode())
    return item


def indexed(fs: MemoryFS, relative: str) -> bool:
    parts = relative.split("/")
    for depth in range(len(parts) - 1, -1, -1):
        parent = "/".join(parts[:depth])
        target = (
            relative if depth == len(parts) - 1 else index.index_path("/".join(parts[: depth + 1]))
        )
        path = fs.path(index.index_path(parent))
        if not path.exists() or target not in index.links(
            fs.raw(path, fs.max_bytes if not parent else FILE_BYTES).decode("utf-8")
        ):
            return False
    return True


def maintenance_resolved(fs: MemoryFS, issue: dict[str, str]) -> bool:
    if not issue:
        return True
    parent, target = issue.get("path"), issue.get("target")
    if not parent or not target:
        return False
    with fs.io():
        text = fs.raw(fs.path(target)).decode("utf-8")
        name, description = (
            (PurePosixPath(target).parent.name, summary(text))
            if target.endswith("/MEMORY.md")
            else metadata(text)
        )
        entries = [
            match[0]
            for match in index.link_matches(
                fs.raw(
                    fs.path(parent), fs.max_bytes if parent == "MEMORY.md" else FILE_BYTES
                ).decode("utf-8")
            )
            if match["target"] == target
        ]
        return len(entries) == 1 and entries[0] in {
            index.entry_text(target, name, description),
            index.managed_entry(target, name, description),
        }
