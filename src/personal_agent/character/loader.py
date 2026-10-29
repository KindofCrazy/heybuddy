"""Local persona.json descriptions, with read-only compatibility for older Markdown roles."""

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
from personal_agent.character.persona import parse_persona

PACKAGE_ROOT = Path(__file__).parent / "packages"
FILES = ("character.md", "behavior.md", "examples.md", "lore.md")
SOUL_FILES = ("soul.md", "lore.md")
SOUL_SECTIONS = ("身份", "性格", "说话方式", "背景", "兴趣", "情绪反应", "相处方式", "存在方式")
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")


def guard(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise CharacterError("linked_package_path")
        if part.exists():
            info = part.stat(follow_symlinks=False)
            if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise CharacterError("linked_package_path")


def sections(text: str, *, level: int = 2) -> list[Section]:
    matches = list(re.finditer(r"(?m)^" + "#" * level + r" ([^\r\n]+)\s*$", text))
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
            guard(directory / "persona.json")
            if (directory / "persona.json").exists():
                return self._persona(character_id, directory)
            guard(directory / "soul.md")
            if (directory / "soul.md").exists() and not (directory / "soul.md").is_file():
                raise CharacterError("invalid_package_text")
            files = SOUL_FILES if (directory / "soul.md").is_file() else FILES
            for name in files:
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
        if "soul.md" in content:
            return self._soul(character_id, content)
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

    def _read(self, path: Path) -> str:
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
        return text

    def _persona(self, character_id: str, directory: Path) -> CharacterPackage:
        content = {"persona.json": self._read(directory / "persona.json")}
        persona = parse_persona(content["persona.json"])
        guide = persona["行为指南"]
        if guide.startswith("file:"):
            # Support the preset's fixed local resource, without following arbitrary references.
            if guide != "file:soul/behavior-guide.md":
                raise CharacterError("invalid_persona_guide")
            content["soul/behavior-guide.md"] = self._read(directory / "soul/behavior-guide.md")
            persona["行为指南"] = content["soul/behavior-guide.md"].strip()
        elif guide.startswith(("https:", "http:")):
            raise CharacterError("invalid_persona_guide")
        return CharacterPackage(
            character_id=character_id,
            character="\n\n".join(value for key, value in persona.items() if key != "行为指南"),
            behavior=persona["行为指南"],
            examples=[],
            lore=[],
            persona=persona,
            fingerprint=hashlib.sha256(
                b"".join(name.encode() + b"\x00" + text.encode() for name, text in content.items())
            ).hexdigest(),
        )

    def _soul(self, character_id: str, content: dict[str, str]) -> CharacterPackage:
        parsed = sections(content["soul.md"], level=1)
        titles = [s.title for s in parsed]
        if len(titles) != len(set(titles)):
            raise CharacterError("duplicate_package_sections")
        profile = {s.title: s.text.partition("\n")[2].strip() for s in parsed}
        if not all(profile.get(title) for title in SOUL_SECTIONS):
            raise CharacterError("character_sections_missing")
        if content["soul.md"].split("# ", 1)[0].strip():
            raise CharacterError("invalid_package_text")
        examples = sections(profile.pop("表达参考", ""))
        lore = sections(content["lore.md"])
        for items in (examples, lore):
            if len({s.title for s in items}) != len(items):
                raise CharacterError("duplicate_package_sections")
        if len(examples) < 2 or not lore:
            raise CharacterError("package_sections_missing")
        character = "\n\n".join(
            f"## {title}\n\n{body}" for title, body in profile.items() if title != "情绪反应"
        )
        return CharacterPackage(
            character_id=character_id,
            character=character,
            behavior=profile["情绪反应"],
            examples=examples,
            lore=lore,
            profile=profile,
            fingerprint=hashlib.sha256(
                b"".join(name.encode() + b"\x00" + content[name].encode() for name in SOUL_FILES)
            ).hexdigest(),
        )
