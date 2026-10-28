"""Character compilation boundary; Core knows no package files or state database."""

import json
from typing import Protocol, runtime_checkable

from personal_agent.core.budget import ContextError
from personal_agent.core.models import TextMessage


class CharacterContext(Protocol):
    def compile(
        self, user_input: str, *, source_run_ids: tuple[str, ...] = ()
    ) -> list[TextMessage]: ...
    def diagnostics(self) -> dict: ...


@runtime_checkable
class RunCharacter(Protocol):
    def record_run(self, run_id: str) -> None: ...


def without_legacy_persona(compiled: str) -> str:
    """Filter only the injection view, preserving MemoryFS storage/compile semantics."""
    try:
        data = json.loads(compiled)
    except (ValueError, TypeError):
        raise ContextError(
            "character_core_contract", "Character mode needs structured Core data."
        ) from None
    if (
        not isinstance(data, dict)
        or data.get("kind") != "agent_core_memory"
        or not isinstance(data.get("blocks"), dict)
    ):
        raise ContextError("character_core_contract", "Character mode needs structured Core data.")
    data["blocks"].pop("persona.md", None)
    data["legacy_persona"] = "preserved_on_disk_not_active_identity"
    return json.dumps(data, ensure_ascii=False)
