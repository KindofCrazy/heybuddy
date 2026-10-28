"""Conservative credential pattern checks, not a general sensitive-data classifier."""

import re

from personal_agent.core.evidence import SECRET

PRIVATE = re.compile(
    r"(?:密码|口令|密钥|访问令牌)\s*(?:是|为|[:：=])\s*\S+|"
    r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{12,}|\bAKIA[A-Z0-9]{16}\b",
    re.I,
)


def contains_credentials(text: str) -> bool:
    return bool(SECRET.search(text) or PRIVATE.search(text))
