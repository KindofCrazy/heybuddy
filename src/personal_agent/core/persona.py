"""Recognize relationship memory without reinterpreting a legacy identity document."""

PERSONA_HEADER = "# 相处记忆\n"


def relationship_persona(text: str) -> bool:
    """Empty legacy templates may receive their first evidenced relationship update."""
    stripped = text.strip()
    return stripped in {"", "# Persona"} or stripped.splitlines()[0] == PERSONA_HEADER.strip()
