import hashlib
import re
from pathlib import Path

import yaml
from pydantic import Field

from personal_agent.tools.registry import Tool, ToolArguments, ToolExecutionError


def metadata(text: str):
    try:
        if any(
            isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
            for token in yaml.scan(text)
        ):
            raise ToolExecutionError("skill_metadata", "YAML aliases are not supported.")
        return yaml.safe_load(text)
    except yaml.YAMLError:
        raise ToolExecutionError("skill_metadata", "Invalid skill metadata.") from None


class SkillName(ToolArguments):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")


class SkillResource(SkillName):
    path: str = Field(min_length=1, max_length=256)


class SkillLoader:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        self.catalog: dict[str, dict] = {}
        self.scan()

    def checked(self, path: Path) -> bytes:
        if not path.resolve().is_relative_to(self.root.resolve()) or any(
            item.is_symlink() or item.is_junction() for item in [path, *path.parents]
        ):
            raise ToolExecutionError("skill_path_denied", "Skill path is not allowed.")
        if not path.is_file() or path.stat().st_size > 32768 or path.stat().st_nlink > 1:
            raise ToolExecutionError("skill_limit", "Skill file is unavailable or too large.")
        return path.read_bytes()

    def scan(self) -> None:
        self.catalog.clear()
        if not self.root.exists():
            return
        manifests = sorted(self.root.glob("*/SKILL.md"))
        if len(manifests) > 100:
            raise ToolExecutionError("skill_limit", "At most 100 installed skills are supported.")
        for path in manifests:
            body = self.checked(path).decode("utf-8")
            pieces = body.split("---", 2)
            if len(pieces) != 3 or pieces[0].strip():
                raise ToolExecutionError("skill_metadata", "Skill requires YAML frontmatter.")
            fields = metadata(pieces[1])
            if not isinstance(fields, dict):
                raise ToolExecutionError("skill_metadata", "Invalid skill metadata.")
            name, description = fields.get("name"), fields.get("description")
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name)
                or name in self.catalog
            ):
                raise ToolExecutionError("skill_metadata", "Invalid or duplicate skill name.")
            if not isinstance(description, str) or not 1 <= len(description) <= 500:
                raise ToolExecutionError("skill_metadata", "Invalid skill description.")
            self.catalog[name] = dict(name=name, description=description, path=path)

    def context(self) -> dict:
        return dict(
            kind="skill_catalog",
            trust="operational_data_only",
            skills=[
                {"name": item["name"], "description": item["description"]}
                for item in self.catalog.values()
            ],
        )

    def load(self, args: SkillName) -> dict:
        if args.name not in self.catalog:
            raise ToolExecutionError("skill_not_found", "Skill is not installed.")
        body = self.checked(self.catalog[args.name]["path"])
        return dict(
            name=args.name,
            text=body.decode("utf-8"),
            hash=hashlib.sha256(body).hexdigest(),
            trust="operational_data_only",
        )

    def resource(self, args: SkillResource) -> dict:
        if args.name not in self.catalog:
            raise ToolExecutionError("skill_not_found", "Skill is not installed.")
        relative = Path(args.path)
        if (
            relative.is_absolute()
            or relative.drive
            or any(p.startswith(".") or ":" in p for p in relative.parts)
        ):
            raise ToolExecutionError("skill_path_denied", "Invalid skill resource path.")
        root = self.catalog[args.name]["path"].parent
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()) or path.suffix.lower() not in {
            ".md",
            ".txt",
            ".json",
        }:
            raise ToolExecutionError("skill_path_denied", "Only local text resources are readable.")
        body = self.checked(path)
        return dict(text=body.decode("utf-8"), hash=hashlib.sha256(body).hexdigest())

    def register(self, registry) -> None:
        from personal_agent.planning.service import Empty

        for name, arguments, function in (
            ("skill_list", Empty, lambda a: self.context()),
            ("skill_load", SkillName, self.load),
            ("skill_read_resource", SkillResource, self.resource),
        ):

            async def invoke(args, function=function):
                return function(args)

            registry.register(
                Tool(name, "Read installed operational skills: " + name, arguments, invoke)
            )
