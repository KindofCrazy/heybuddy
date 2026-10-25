"""Fail closed before writing private memory into a visible Git directory."""

import os
import subprocess
from pathlib import Path

from personal_agent.core.memory import CoreMemoryError


def require_private_directory(path: Path) -> None:
    """Require a wholly ignored, untracked subtree in a normal Git repo/worktree."""
    target = path.resolve()
    for directory in (target, *target.parents):
        if (directory / ".git").exists():
            relative = target.relative_to(directory)
            if relative == Path(".") or relative.parts[0] == ".git":
                raise CoreMemoryError(
                    "memory_not_private", "Memory cannot use Git metadata or root."
                )
            # Do not let inherited Git redirection inspect a different repository/index.
            environment = {
                key: value for key, value in os.environ.items() if not key.startswith("GIT_")
            }
            command = ["git", "-C", str(directory)]
            try:
                tracked = subprocess.run(
                    [
                        *command,
                        "--literal-pathspecs",
                        "ls-files",
                        "--cached",
                        "-z",
                        "--",
                        relative.as_posix(),
                    ],
                    capture_output=True,
                    timeout=2,
                    env=environment,
                    check=False,
                )
                ignored = subprocess.run(
                    [
                        *command,
                        "check-ignore",
                        "--quiet",
                        "--no-index",
                        "--",
                        relative.as_posix() + "/",
                    ],
                    capture_output=True,
                    timeout=2,
                    env=environment,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise CoreMemoryError(
                    "memory_privacy_error",
                    "Cannot verify Git memory privacy; no memory was written.",
                ) from None
            if tracked.returncode != 0 or ignored.returncode not in {0, 1}:
                raise CoreMemoryError(
                    "memory_privacy_error",
                    "Cannot verify Git memory privacy; no memory was written.",
                )
            if tracked.stdout or ignored.returncode != 0:
                raise CoreMemoryError(
                    "memory_not_private",
                    "Memory in Git must be wholly ignored and contain no tracked files. "
                    "Move it outside the repo or ignore the directory and untrack existing files.",
                )
            return
