"""Packaged, character-independent defaults for main-chat expression only."""

from pathlib import Path


def _load_conversation_prompt() -> str:
    path = Path(__file__).parents[1] / "prompts" / "conversation.md"
    try:
        with path.open("rb") as stream:
            raw = stream.read(8193)
        if not raw or len(raw) > 8192:
            raise ValueError
        text = raw.decode("utf-8-sig").strip()
        if not text or "\x00" in text:
            raise ValueError
        return text
    except (OSError, UnicodeError, ValueError):
        raise RuntimeError("Main conversation prompt is missing or invalid.") from None


CONVERSATION_PROMPT = _load_conversation_prompt()


def with_conversation_prompt(system: str) -> str:
    """Compose once at the default-system/character boundary, before request budgeting."""
    return "\n\n".join(part for part in (system, CONVERSATION_PROMPT) if part)
