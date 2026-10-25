import asyncio
import subprocess

import pytest

from personal_agent import main as cli
from personal_agent.config import Settings
from personal_agent.core.memory import CoreMemoryError
from personal_agent.memory.store import MemoryStore


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repository"
    path.mkdir()
    git(path, "init", "--quiet")
    return path


def test_unignored_custom_directory_is_rejected_before_private_files_are_created(repo):
    memory = MemoryStore.for_agent(repo / "private", "heybuddy")
    with pytest.raises(CoreMemoryError) as caught:
        memory.initialize()
    assert caught.value.code == "memory_not_private"
    assert not memory.root.exists()
    assert not (repo / ".gitignore").exists()


def test_wholly_ignored_custom_directory_remains_hidden_after_update_and_restore(repo):
    (repo / ".gitignore").write_text("/private/\n")
    memory = MemoryStore.for_agent(repo / "private", "heybuddy")
    first = memory.initialize()
    changed = memory.edit(
        "human.md", "# Human", "# Human\nC", expected_version=first.revision.version
    )
    memory.restore(first.revision.version, expected_version=changed.revision.version)
    assert not git(repo, "status", "--porcelain", "--untracked-files=all", "--", "private").stdout
    assert (repo / ".gitignore").read_text() == "/private/\n"


def test_tracked_files_are_rejected_even_when_directory_is_ignored(repo, tmp_path, monkeypatch):
    directory = repo / "private" / "heybuddy"
    directory.mkdir(parents=True)
    (directory / "human.md").write_text("existing private data")
    git(repo, "add", "--", "private/heybuddy/human.md")
    (repo / ".gitignore").write_text("/private/\n")
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "unrelated-empty-index"))
    memory = MemoryStore(directory)
    with pytest.raises(CoreMemoryError) as caught:
        memory.initialize()
    assert caught.value.code == "memory_not_private"
    assert (directory / "human.md").read_text() == "existing private data"
    assert not (directory / "CURRENT").exists()
    monkeypatch.delenv("GIT_INDEX_FILE")
    assert git(repo, "ls-files", "--", "private").stdout


def test_ignore_revocation_stops_further_writes_without_changing_snapshot(repo):
    ignore = repo / ".gitignore"
    ignore.write_text("/private/\n")
    memory = MemoryStore.for_agent(repo / "private", "heybuddy")
    before = memory.initialize()
    ignore.write_text("")
    with pytest.raises(CoreMemoryError) as caught:
        memory.edit("human.md", "# Human", "C", expected_version=before.revision.version)
    assert caught.value.code == "memory_not_private"
    assert memory.read() == before


def test_git_worktree_marker_file_is_supported(repo, tmp_path):
    worktree = tmp_path / "worktree"
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--quiet",
        "--allow-empty",
        "-m",
        "test fixture",
    )
    git(repo, "worktree", "add", "--quiet", "--detach", str(worktree))
    (worktree / ".gitignore").write_text("/private/\n")
    memory = MemoryStore.for_agent(worktree / "private", "heybuddy")
    assert memory.initialize().blocks["human.md"] == "# Human\n"


def test_git_unavailable_fails_closed_without_leaking_subprocess_output(repo, monkeypatch):
    memory = MemoryStore.for_agent(repo / "private", "heybuddy")

    def fail(*args, **kwargs):
        raise FileNotFoundError("private-secret-path")

    monkeypatch.setattr("personal_agent.memory.privacy.subprocess.run", fail)
    with pytest.raises(CoreMemoryError) as caught:
        memory.initialize()
    assert caught.value.code == "memory_privacy_error"
    assert "private-secret" not in str(caught.value)
    assert not memory.root.exists()


def test_custom_cli_root_is_checked_before_sdk_is_created(repo, tmp_path, monkeypatch):
    settings = Settings(
        api_key="test-key",
        model="test-model",
        database_path=tmp_path / "history.sqlite3",
        memory_root=repo / "private",
    )

    def unexpected(*args, **kwargs):
        pytest.fail("SDK must not be constructed for an unsafe memory directory")

    monkeypatch.setattr(cli, "AsyncOpenAI", unexpected)
    with pytest.raises(CoreMemoryError) as caught:
        asyncio.run(cli.run_cli(settings))
    assert caught.value.code == "memory_not_private"
    assert not settings.memory_root.exists()


def test_cli_privacy_error_is_actionable_and_does_not_expose_path(
    repo, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr("sys.argv", ["personal-agent"])
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("AGENT_DATABASE_PATH", str(tmp_path / "history.sqlite3"))
    monkeypatch.setenv("AGENT_MEMORY_ROOT", str(repo / "private-sensitive-path"))
    assert cli.main() == 1
    error = capsys.readouterr().err
    assert "memory_not_private" in error and "Git" in error
    assert "private-sensitive-path" not in error
