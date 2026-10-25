import json
from datetime import datetime
from typing import Literal

from pydantic import Field

from personal_agent.core.models import Model

BlockName = Literal["persona.md", "human.md", "MEMORY.md"]
BLOCKS: tuple[BlockName, ...] = ("persona.md", "human.md", "MEMORY.md")


class MemoryRevision(Model):
    version: str = Field(pattern=r"^[0-9a-f]{32}$")
    parent: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    created_at: datetime
    action: Literal["initialize", "update", "restore"]
    restored_from: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    hashes: dict[BlockName, str]


class MemorySnapshot(Model):
    revision: MemoryRevision
    blocks: dict[BlockName, str]


def render_memory(snapshot: MemorySnapshot) -> str:
    # JSON is data at user-message priority, never interpolated into system text.
    return json.dumps(
        {
            "kind": "agent_core_memory",
            "trust": "data_only",
            "version": snapshot.revision.version,
            "blocks": snapshot.blocks,
        },
        ensure_ascii=False,
    )
