"""Bounded document workspace, exact edits, and host-registered command profiles."""

import asyncio
import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field

from personal_agent.core.text_window import text_window
from personal_agent.execution.process import ProcessTree
from personal_agent.tools.registry import Tool, ToolArguments, ToolExecutionError

EXTENSIONS = {".md", ".txt", ".json", ".csv", ".tsv"}
FORBIDDEN = {"memory", "memories", "characters", "skills", "node_modules", "__pycache__"}


class WorkspacePath(ToolArguments):
    path: str = Field(default="", max_length=512)


class WorkspaceRead(WorkspacePath):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8192, ge=1, le=16384)


class WorkspaceSearch(ToolArguments):
    query: str = Field(min_length=1, max_length=200)


class WorkspaceWrite(WorkspacePath):
    content: str = Field(max_length=65536)
    expected_hash: str | None = None


class WorkspaceEdit(WorkspacePath):
    old_text: str = Field(min_length=1, max_length=65536)
    new_text: str = Field(max_length=65536)
    expected_hash: str


class CommandRun(ToolArguments):
    command_id: Literal["text_search", "git_status"]
    query: str = Field(default="", max_length=200)


class Workspace:
    def __init__(
        self, root: Path, *, git_root: Path | None = None, protected_roots: tuple[Path, ...] = ()
    ) -> None:
        self.root = root.absolute()
        self.git_root = git_root
        self.protected_roots = protected_roots
        self._lock = asyncio.Lock()

    def path(self, value: str, *, directory: bool = False) -> Path:
        candidate = Path(value)
        parts = candidate.parts
        if (
            candidate.is_absolute()
            or candidate.drive
            or any(
                p in {"..", "."}
                or p.startswith(".")
                or ":" in p
                or p.endswith((" ", "."))
                or p.lower() in FORBIDDEN
                or p.split(".")[0].upper()
                in {
                    "CON",
                    "PRN",
                    "AUX",
                    "NUL",
                    *(f"COM{i}" for i in range(10)),
                    *(f"LPT{i}" for i in range(10)),
                }
                for p in parts
            )
        ):
            raise ToolExecutionError("path_denied", "Path is outside the document policy.")
        target = self.root / candidate
        if (
            target.name.lower() in {"agents.md", "skill.md", "memory.md", "persona.md", "human.md"}
            or any(
                term in target.stem.lower()
                for term in ("credential", "secret", "password", "config", "settings", "token")
            )
            or any(target.resolve().is_relative_to(root.resolve()) for root in self.protected_roots)
        ):
            raise ToolExecutionError("path_denied", "Protected configuration or memory path.")
        for parent in [target, *target.parents]:
            if parent.exists() and (parent.is_symlink() or parent.is_junction()):
                raise ToolExecutionError("path_denied", "Linked workspace paths are forbidden.")
        if not target.resolve().is_relative_to(self.root.resolve()):
            raise ToolExecutionError("path_denied", "Path escapes the workspace.")
        if not directory and target.suffix.lower() not in EXTENSIONS:
            raise ToolExecutionError("file_type_denied", "Only working document types are allowed.")
        return target

    @staticmethod
    def contents(path: Path) -> bytes:
        if not path.is_file() or path.stat().st_size > 1048576:
            raise ToolExecutionError("file_limit", "File is missing or exceeds the read limit.")
        if path.stat().st_nlink > 1:
            raise ToolExecutionError("path_denied", "Hard-linked files are forbidden.")
        return path.read_bytes()

    def read(self, args: WorkspaceRead) -> dict:
        body = self.contents(self.path(args.path))
        try:
            text, used = text_window(
                body[args.offset : args.offset + args.limit],
                final=args.offset + args.limit >= len(body),
            )
        except (ValueError, UnicodeError):
            raise ToolExecutionError(
                "invalid_window", "Choose a UTF-8 character boundary and sufficient limit."
            ) from None
        return dict(
            path=args.path,
            hash=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            text=text,
            next_offset=args.offset + used if args.offset + used < len(body) else None,
        )

    def list(self, args: WorkspacePath) -> dict:
        root = self.path(args.path, directory=True)
        if not root.exists():
            return {"entries": []}
        entries = []
        for path in root.iterdir():
            if len(entries) >= 100:
                break
            try:
                self.path(str(path.relative_to(self.root)), directory=path.is_dir())
            except ToolExecutionError:
                continue
            entries.append(
                dict(path=path.relative_to(self.root).as_posix(), directory=path.is_dir())
            )
        return dict(entries=entries, truncated=len(entries) == 100)

    def search(self, args: WorkspaceSearch) -> dict:
        matches, scanned = [], 0
        if not self.root.exists():
            return dict(matches=[], scope_limited=False)
        # Walk only allowed directories; never recurse into hidden/linked trees.
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            dirs[:] = [
                name
                for name in dirs
                if not name.startswith(".")
                and name.lower() not in FORBIDDEN
                and not (Path(directory) / name).is_junction()
            ]
            for name in files:
                scanned += 1
                if scanned > 200 or len(matches) >= 30:
                    return dict(matches=matches, scope_limited=True)
                path = Path(directory) / name
                try:
                    relative = path.relative_to(self.root).as_posix()
                    body = self.contents(self.path(relative)).decode("utf-8")
                except (ToolExecutionError, OSError, UnicodeError):
                    continue
                at = body.find(args.query)
                if at >= 0:
                    matches.append(dict(path=relative, text=body[max(0, at - 80) : at + 240]))
        return dict(matches=matches, scope_limited=False)

    async def write(self, args: WorkspaceWrite | WorkspaceEdit) -> dict:
        # No await during the final hash check / atomic filesystem publication.
        async with self._lock:
            path = self.path(args.path)
            previous = self.contents(path) if path.exists() else None
            expected = args.expected_hash
            actual = hashlib.sha256(previous).hexdigest() if previous is not None else None
            if actual != expected:
                raise ToolExecutionError("hash_conflict", "Working file changed; reread it first.")
            if isinstance(args, WorkspaceEdit):
                if previous is None:
                    raise ToolExecutionError("not_found", "Working file does not exist.")
                text = previous.decode("utf-8")
                if text.count(args.old_text) != 1:
                    raise ToolExecutionError("edit_conflict", "Old text must match exactly once.")
                content = text.replace(args.old_text, args.new_text, 1)
            else:
                content = args.content
            body = content.encode()
            if len(body) > 65536:
                raise ToolExecutionError("file_limit", "Working document exceeds write budget.")
            path.parent.mkdir(parents=True, exist_ok=True)
            self.path(args.path)
            fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".publish-")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                if path.exists():
                    if hashlib.sha256(self.contents(path)).hexdigest() != expected:
                        raise ToolExecutionError(
                            "hash_conflict", "Working file changed before publish."
                        )
                    os.replace(temporary, path)
                else:
                    # Hard-link creation is exclusive: never overwrite a concurrent create.
                    os.link(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)  # noqa: ASYNC240 - short atomic publication
            return dict(path=args.path, hash=hashlib.sha256(body).hexdigest(), size_bytes=len(body))

    async def command(self, args: CommandRun) -> dict:
        if args.command_id == "text_search":
            return self.search(WorkspaceSearch(query=args.query))
        if self.git_root is None:
            raise ToolExecutionError("command_denied", "No repository is granted by the Host.")
        executable = shutil.which("git")
        if executable is None:
            raise ToolExecutionError("command_unavailable", "Git is unavailable.")
        process = await asyncio.create_subprocess_exec(
            executable,
            "--no-optional-locks",
            "-c",
            "core.fsmonitor=false",
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
            cwd=self.git_root,
            env={key: os.environ[key] for key in ("SystemRoot", "PATH") if key in os.environ},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=0x08000000 if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        tree = None
        try:
            tree = ProcessTree(process)
            async with asyncio.timeout(5):
                output = bytearray()
                truncated = False
                while chunk := await process.stdout.read(16384):
                    remaining = 16384 - len(output)
                    output.extend(chunk[:remaining])
                    truncated |= len(chunk) > remaining
                await process.wait()
        finally:
            if tree is not None:
                tree.close()
            if process.returncode is None:
                process.kill()
                await process.wait()
        return dict(
            exit_code=process.returncode,
            text=output[:16384].decode("utf-8", "replace"),
            truncated=truncated,
        )

    def register(self, registry) -> None:
        for name, args, operation in (
            ("workspace_list", WorkspacePath, self.list),
            ("workspace_read", WorkspaceRead, self.read),
            ("workspace_search", WorkspaceSearch, self.search),
        ):

            async def invoke(value, operation=operation):
                return operation(value)

            registry.register(Tool(name, "Read working documents: " + name, args, invoke))
        registry.register(
            Tool(
                "workspace_write",
                "Create or hash-checked replace a working document.",
                WorkspaceWrite,
                self.write,
            )
        )
        registry.register(
            Tool(
                "workspace_edit", "Apply one exact unique document edit.", WorkspaceEdit, self.write
            )
        )
        registry.register(
            Tool(
                "command_run",
                "Run a Host-registered command profile, never shell code.",
                CommandRun,
                self.command,
            )
        )
