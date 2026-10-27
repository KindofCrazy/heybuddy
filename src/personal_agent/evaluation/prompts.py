"""Evaluation-only prompt snapshots. Never export credentials or user memory."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from personal_agent.automation import service as automation
from personal_agent.core import compaction, context
from personal_agent.memory.store import MemoryFS
from personal_agent.memory.tools import register_memory_tools
from personal_agent.recall.service import Recall
from personal_agent.recall.tools import register_recall_tools
from personal_agent.reflection import service as reflection
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.tools.builtin import create_builtin_registry

PERMISSIONS = frozenset({"memory.read", "memory.update", "deferred.update"})


def capture(folder: Path) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    with SQLiteStore(folder / "snapshot.sqlite3") as db:
        db.initialize()
        registry = create_builtin_registry()
        register_memory_tools(registry, MemoryFS.for_agent(folder / "memory", "heybuddy"))
        register_recall_tools(registry, Recall(db, "heybuddy"))
        return {
            "main": context.DEFAULT_SYSTEM_PROMPT,
            "summary": compaction.SUMMARY_PROMPT,
            "worthiness": automation.WORTHINESS_PROMPT,
            "reflection": {
                key: getattr(reflection, key)
                for key in ("BASE", "EXTRACT", "SELECT", "CONSOLIDATE")
            },
            "tools": {
                definition.name: definition.description
                for definition in registry.definitions(permissions=PERMISSIONS)
            },
        }


def digest_snapshot(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def install(value: dict) -> None:
    compaction.SUMMARY_PROMPT = value["summary"]
    automation.WORTHINESS_PROMPT = value["worthiness"]
    for key, prompt in value["reflection"].items():
        setattr(reflection, key, prompt)
    automation.EXTRACT = reflection.EXTRACT


def tool_variant(registry, value: dict) -> None:
    for name, tool in registry._tools.items():
        if name in value["tools"]:
            registry._tools[name] = replace(tool, description=value["tools"][name])
