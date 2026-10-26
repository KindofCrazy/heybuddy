"""Plain Markdown data, content hashes and bounded output; no revision history."""

import hashlib
import json
import re
from typing import Literal

from personal_agent.core.memory import CoreMemoryError
from personal_agent.core.models import Model

BlockName = Literal["persona.md", "human.md", "MEMORY.md"]
BLOCKS: tuple[BlockName, ...] = ("persona.md", "human.md", "MEMORY.md")
FILE_BYTES = 65536
OUTPUT_BYTES = 16384
MAX_DEPTH = 3
MAX_ENTRIES = 500


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class CoreMemoryData(Model):
    blocks: dict[BlockName, str]
    hashes: dict[BlockName, str]


def render_memory(data: CoreMemoryData) -> str:
    return json.dumps(
        {"kind": "agent_core_memory", "trust": "data_only", "blocks": data.blocks},
        ensure_ascii=False,
    )


def metadata(text: str) -> tuple[str, str]:
    """A small frontmatter subset: two single-line string scalars, no YAML dependency."""
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise CoreMemoryError(
            "invalid_metadata", "Knowledge files need name/description frontmatter."
        )
    fields = {}
    for line in lines[1:]:
        if line == "---":
            break
        key, separator, value = line.partition(":")
        if not separator or key not in {"name", "description"} or key in fields:
            raise CoreMemoryError("invalid_metadata", "Use exactly name and description fields.")
        value = value.strip()
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                raise CoreMemoryError("invalid_metadata", "Invalid quoted metadata.") from None
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("''", "'")
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > (80 if key == "name" else 240)
            or any(ord(char) < 32 for char in value)
        ):
            raise CoreMemoryError("invalid_metadata", "Metadata must be short nonempty strings.")
        fields[key] = value
    else:
        raise CoreMemoryError("invalid_metadata", "Frontmatter has no closing delimiter.")
    if set(fields) != {"name", "description"}:
        raise CoreMemoryError("invalid_metadata", "Use exactly name and description fields.")
    return fields["name"], fields["description"]


def summary(text: str) -> str:
    paragraphs = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    return next(
        (
            p.strip()[:240]
            for p in paragraphs
            if p.strip() and not p.lstrip().startswith(("#", "-"))
        ),
        "",
    )


def bounded(data: dict) -> dict:
    if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > OUTPUT_BYTES:
        raise CoreMemoryError("memory_output_limit", "Memory output exceeds its byte limit.")
    return data


def clip(text: str, budget: int) -> str:
    used = 0
    for index, char in enumerate(text):
        used += len(json.dumps(char, ensure_ascii=False)[1:-1].encode("utf-8"))
        if used > budget:
            return text[:index]
    return text
