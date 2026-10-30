"""Local delivery policy; no model calls, content rewriting or transport side effects."""

import random
import re

from personal_agent.channels.qq.config import QQSettings
from personal_agent.channels.qq.events import split_reply

# Be conservative: structured answers retain the existing byte-bound delivery plan.
_STRUCTURED = re.compile(
    r"[`$\[\]]|~~~|https?://|www\.|\\[\[(]"
    r"|^[ \t]*(?:#{1,6}\s|[-+*]\s|\d+[.)、]\s|[>|]| {4}\S)",
    re.MULTILINE,
)
_BREAK = re.compile(r"""[。！？!?]+[”’」』）)\]"]*[ \t]*|\.(?=\s)|\r?\n(?:[ \t]*\r?\n)*""")


class Humanizer:
    def __init__(self, settings: QQSettings) -> None:
        self.settings = settings

    def split(self, text: str) -> list[str]:
        """Never spend natural breaks at the expense of byte-bound content capacity."""
        settings = self.settings
        bounded = split_reply(text, settings.qq_text_max_bytes, settings.qq_reply_max_parts)
        if not settings.qq_humanize_enabled or _STRUCTURED.search(text):
            return bounded
        spare = settings.qq_reply_max_parts - len(bounded)
        result = []
        for chunk in bounded:
            fragments = []
            start = 0
            paragraph_boundary = False
            for match in _BREAK.finditer(chunk):
                fragment = chunk[start : match.end()]
                if fragments and (
                    not fragment.strip()
                    or (not paragraph_boundary and len(fragments[-1].strip()) < 6)
                ):
                    fragments[-1] += fragment
                else:
                    fragments.append(fragment)
                # Blank lines explicitly separate bubbles, even after a short greeting.
                paragraph_boundary = match.group().count("\n") >= 2
                start = match.end()
            if start < len(chunk):
                tail = chunk[start:]
                if fragments and not paragraph_boundary and len(fragments[-1].strip()) < 6:
                    fragments[-1] += tail
                else:
                    fragments.append(tail)
            fragments = [part for part in fragments if part.strip()]
            count = min(spare + 1, len(fragments))
            # Merge overflow using original whitespace, not a newly inserted separator.
            parts = fragments[: count - 1] + ["".join(fragments[count - 1 :])]
            result.extend(part.strip() for part in parts)
            spare -= len(parts) - 1
        return result

    def delay(self, text: str, *, first: bool, elapsed: float = 0) -> float:
        """Desired pause, with generation time credited against the first part."""
        settings = self.settings
        if not settings.qq_humanize_enabled:
            return 0.0
        base = (
            len(text) * settings.qq_humanize_ms_per_char / 1000
            if first
            else settings.qq_humanize_gap_seconds
        )
        factor = random.uniform(1 - settings.qq_humanize_jitter, 1 + settings.qq_humanize_jitter)
        delay = min(settings.qq_humanize_max_delay_seconds, base * factor)
        return max(0.0, delay - max(0.0, elapsed)) if first else delay
