"""Fixed-name, read-only local Markdown packages; no code, network or dynamic installation."""

import hashlib
import re
import stat
from pathlib import Path

from personal_agent.character.models import (
    CharacterError,
    CharacterPackage,
    CharacterPolicy,
    Section,
)

PACKAGE_ROOT = Path(__file__).parent / "packages"
FILES = ("character.md", "behavior.md", "examples.md", "lore.md")
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")


def guard(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise CharacterError("linked_package_path")
        if part.exists():
            info = part.stat(follow_symlinks=False)
            if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise CharacterError("linked_package_path")


def sections(text: str) -> list[Section]:
    matches = list(re.finditer(r"(?m)^## ([^\r\n]+)\s*$", text))
    return [
        Section(
            title=m[1].strip(),
            text=text[
                m.start() : matches[n + 1].start() if n + 1 < len(matches) else len(text)
            ].strip(),
        )
        for n, m in enumerate(matches)
    ]


class CharacterLoader:
    def __init__(self, root: Path | None = None, policy: CharacterPolicy | None = None) -> None:
        self.root = (root if root is not None else PACKAGE_ROOT).absolute()
        self.policy = policy or CharacterPolicy()

    def load(self, character_id: str) -> CharacterPackage:
        if not IDENTIFIER.fullmatch(character_id) or character_id == "none":
            raise CharacterError("invalid_character_id")
        directory = self.root / character_id
        content = {}
        try:
            guard(directory)
            if not directory.is_dir():
                raise CharacterError("character_not_found")
            for name in FILES:
                path = directory / name
                guard(path)
                if not path.is_file():
                    raise CharacterError("package_file_missing")
                with path.open("rb") as stream:
                    raw = stream.read(self.policy.max_file_bytes + 1)
                if len(raw) > self.policy.max_file_bytes:
                    raise CharacterError("package_file_budget")
                text = raw.decode("utf-8-sig").replace("\r\n", "\n")
                if not text.strip() or "\x00" in text:
                    raise CharacterError("invalid_package_text")
                content[name] = text
        except (OSError, UnicodeError):
            raise CharacterError("package_read_failed") from None
        required = {
            "Identity",
            "Core Personality",
            "Flaws & Contradictions",
            "Character Adaptation",
        }
        for name in FILES:
            titles = [section.title for section in sections(content[name])]
            if len(titles) != len(set(titles)):
                raise CharacterError("duplicate_package_sections")
        if not required <= {s.title for s in sections(content["character.md"])}:
            raise CharacterError("character_sections_missing")
        if (
            len(sections(content["behavior.md"])) < 2
            or len(sections(content["examples.md"])) < 2
            or not sections(content["lore.md"])
        ):
            raise CharacterError("package_sections_missing")
        fingerprint = hashlib.sha256(
            b"".join(name.encode() + b"\x00" + content[name].encode() for name in FILES)
        ).hexdigest()
        return CharacterPackage(
            character_id=character_id,
            character=content["character.md"],
            behavior=content["behavior.md"],
            examples=sections(content["examples.md"]),
            lore=sections(content["lore.md"]),
            fingerprint=fingerprint,
        )
