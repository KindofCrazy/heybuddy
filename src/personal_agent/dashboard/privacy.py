"""Presentation-only redaction; original journal and Markdown remain untouched."""

import re

from personal_agent.core.evidence import SECRET
from personal_agent.memory.safety import PRIVATE

SENSITIVE = re.compile(
    r"api[_ -]?key|app[_ -]?secret|access[_ -]?token|authorization|password|credential|"
    r"(?:^|_)(?:token|secret)$|openid|session_id|\.env|database_path|memory_root",
    re.I,
)
ASSIGNMENT = re.compile(
    r"(?i)\b(?:app[_ -]?secret|access[_ -]?token|api[_ -]?key)\b"
    r"[\"']?\s*[:=]\s*[\"']?[^\s\"',}]+"
)


def safe(value):
    """Bound recursion, collection and text sizes; suppress credential-shaped fields."""
    return _safe(value, 0)


def snapshot_safe(value):
    """Redact a size-bounded stored snapshot before splitting it into HTTP pages."""
    return _safe(value, 0, bounded=False)


def _safe(value, depth, *, bounded=True):
    if depth > 12:
        return "[depth limit]"
    if isinstance(value, str):
        text = ASSIGNMENT.sub(
            "[REDACTED]", PRIVATE.sub("[REDACTED]", SECRET.sub("[REDACTED]", value))
        )
        return text[:16384] + ("\n[truncated]" if len(text) > 16384 else "") if bounded else text
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]"
            if SENSITIVE.search(str(k))
            else _safe(v, depth + 1, bounded=bounded)
            for k, v in (list(value.items())[:128] if bounded else value.items())
            if k not in {"provider_data", "reasoning", "result_json", "agent_id"}
        }
    if isinstance(value, (list, tuple)):
        return [
            _safe(item, depth + 1, bounded=bounded) for item in (value[:128] if bounded else value)
        ]
    return value
