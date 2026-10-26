"""File contracts: editor visibility, optimistic hashes, confinement and atomic publication."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from personal_agent.core.memory import CoreMemoryError
from personal_agent.memory.models import BLOCKS
from personal_agent.memory.store import MemoryFS


@pytest.fixture
def memory(tmp_path):
    fs = MemoryFS.for_agent(tmp_path / "memory", "heybuddy")
    fs.initialize()
    return fs


def test_plain_markdown_is_authoritative_and_reopen_is_idempotent(memory):
    before = memory.read()
    assert set(before.blocks) == set(BLOCKS)
    assert not (memory.root / "CURRENT").exists()
    assert not (memory.root / "versions").exists()
    for block in BLOCKS:
        assert (memory.root / block).read_bytes().decode() == before.blocks[block]
    (memory.root / "human.md").write_bytes("编辑器直接修改\r\n".encode())
    reopened = MemoryFS(memory.root)
    assert reopened.initialize().blocks["human.md"] == "编辑器直接修改\r\n"
    assert reopened.compile() == memory.compile()
    assert not hasattr(memory, "history") and not hasattr(memory, "restore")


def test_per_agent_isolation(memory, tmp_path):
    other = MemoryFS.for_agent(tmp_path / "memory", "other")
    before = other.initialize()
    memory.update("human.md", "user one", expected_hash=memory.read().hashes["human.md"])
    assert other.read() == before


def test_partial_edit_preserves_unrelated_text(memory):
    before = memory.update("human.md", "A\nB\n", expected_hash=memory.read().hashes["human.md"])
    value = memory.edit("human.md", "B\n", "B\nC\n", expected_hash=before.hashes["human.md"])
    assert memory.read().blocks == {**before.blocks, "human.md": "A\nB\nC\n"}
    assert value == memory.read().hashes["human.md"]


@pytest.mark.parametrize(
    "content,old", [("A B B", "B"), ("aaa", "aa"), ("A", ""), ("A", "missing")]
)
def test_ambiguous_empty_or_missing_spans_leave_file_unchanged(memory, content, old):
    before = memory.update("human.md", content, expected_hash=memory.read().hashes["human.md"])
    with pytest.raises(CoreMemoryError) as caught:
        memory.edit("human.md", old, "C", expected_hash=before.hashes["human.md"])
    assert caught.value.code == "memory_edit_mismatch"
    assert memory.read() == before


def test_external_editor_stale_hash_is_rejected(memory):
    before = memory.read()
    (memory.root / "human.md").write_bytes(b"external editor content")
    with pytest.raises(CoreMemoryError) as caught:
        memory.edit("human.md", "# Human", "lost update", expected_hash=before.hashes["human.md"])
    assert caught.value.code == "memory_conflict"
    assert memory.read().blocks["human.md"] == "external editor content"


def test_external_change_while_temp_is_flushed_is_detected(memory, monkeypatch):
    before = memory.read()
    original = os.fsync

    def editor(fd):
        original(fd)
        (memory.root / "human.md").write_bytes(b"editor won")

    monkeypatch.setattr("personal_agent.memory.store.os.fsync", editor)
    with pytest.raises(CoreMemoryError) as caught:
        memory.update("human.md", "stale", expected_hash=before.hashes["human.md"])
    assert caught.value.code == "memory_conflict"
    assert memory.read().blocks["human.md"] == "editor won"


def test_concurrent_controlled_writers_cannot_lose_an_update(memory):
    expected = memory.read().hashes["human.md"]
    ready = Barrier(2)

    def edit(text):
        fs = MemoryFS(memory.root)
        ready.wait(timeout=3)
        try:
            return fs.update("human.md", text, expected_hash=expected).blocks["human.md"]
        except CoreMemoryError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(edit, ["first", "second"]))
    assert sum(item in {"memory_busy", "memory_conflict"} for item in outcomes) == 1
    assert memory.read().blocks["human.md"] in {"first", "second"}


@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_failed_single_file_write_keeps_previous_bytes(memory, monkeypatch, failure):
    before = memory.read()

    def fail(*args):
        raise OSError("private-path-secret")

    monkeypatch.setattr(f"personal_agent.memory.store.os.{failure}", fail)
    with pytest.raises(CoreMemoryError) as caught:
        memory.update("human.md", "unpublished", expected_hash=before.hashes["human.md"])
    assert caught.value.code == "memory_io_error"
    assert "private-path" not in str(caught.value)
    assert memory.read() == before
    assert not list(memory.root.glob("*.tmp"))


@pytest.mark.parametrize("content", ["你" * 200, "\n" * 400, '"' * 400, "\x00" * 100])
def test_budget_counts_utf8_and_json_escaping(tmp_path, content):
    fs = MemoryFS(tmp_path / "small", max_bytes=512)
    before = fs.initialize()
    with pytest.raises(CoreMemoryError) as caught:
        fs.update("human.md", content, expected_hash=before.hashes["human.md"])
    assert caught.value.code == "memory_too_large"
    assert fs.read() == before


def test_exact_core_budget_and_shared_capacity(tmp_path):
    fs = MemoryFS(tmp_path / "small", max_bytes=512)
    before = fs.initialize()
    escaped = len(json.dumps(before.blocks["human.md"], ensure_ascii=False).encode()) - 2
    overhead = len(fs.compile().encode()) - escaped
    content = "x" * (512 - overhead)
    current = fs.update("human.md", content, expected_hash=before.hashes["human.md"])
    assert len(fs.compile().encode()) == 512
    with pytest.raises(CoreMemoryError):
        fs.update("persona.md", "extra bytes" * 10, expected_hash=current.hashes["persona.md"])
    with pytest.raises(CoreMemoryError):
        fs.update("human.md", content + "x", expected_hash=current.hashes["human.md"])
    assert fs.read() == current


@pytest.mark.parametrize(
    "path",
    [
        "../other/human.md",
        "/tmp/escape.md",
        "C:/escape.md",
        "x\\evil.md",
        "x/../y.md",
        "x//y.md",
        "x/.env",
        "x/nul.md",
        "x/CON.md",
        "x/bad name.md",
        "x/a.md:stream",
        "x/a.md.",
        "x/memory.md",
        "a/b/c/d/e.md",
        "other.md",
        "versions/ab.md",
        ".git/config.md",
    ],
)
def test_paths_reject_escape_and_platform_aliases(memory, path):
    with pytest.raises(CoreMemoryError) as caught:
        memory.read_file(path)
    assert caught.value.code == "invalid_path"


@pytest.mark.parametrize("agent_id", ["../other", "", "nul", "a/b", "C:", "a" * 65, "HeyBuddy"])
def test_agent_ids_cannot_escape_root(tmp_path, agent_id):
    with pytest.raises(CoreMemoryError):
        MemoryFS.for_agent(tmp_path, agent_id)


def test_symlinked_file_or_parent_is_rejected(memory, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "topic.md").write_bytes(b"private")
    directory = memory.root / "linked"
    try:
        directory.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not allow creating symlinks")
    with pytest.raises(CoreMemoryError, match="links"):
        memory.read_file("linked/topic.md")
    with pytest.raises(CoreMemoryError, match="links"):
        MemoryFS.for_agent(directory, "nested").initialize()


def test_hardlink_cannot_read_another_agent(memory, tmp_path):
    directory = memory.root / "topics"
    directory.mkdir()
    outside = tmp_path / "secret.md"
    outside.write_bytes(b"private")
    os.link(outside, directory / "secret.md")
    with pytest.raises(CoreMemoryError, match="hard links"):
        memory.read_file("topics/secret.md")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_windows_junction_is_rejected(memory, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = memory.root / "linked"
    # Fixed test fixture paths; no shell-derived deletion or moving.
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True
    )
    assert result.returncode == 0
    assert junction.is_junction()
    with pytest.raises(CoreMemoryError):
        MemoryFS(junction / "nested").initialize()
    with pytest.raises(CoreMemoryError):
        memory.list_directory()
    junction.rmdir()


@pytest.mark.parametrize("phase", ["before", "after"])
def test_process_exit_at_atomic_replace_keeps_complete_old_or_new_file(memory, phase):
    before = memory.read()
    script = """
import os,sys
from pathlib import Path
from personal_agent.memory.store import MemoryFS
fs=MemoryFS(Path(sys.argv[1]))
replace=os.replace

def crash(source,destination):
    if sys.argv[2]=='after':
        replace(source,destination)
    os._exit(0)
os.replace=crash
fs.update('human.md','child committed',expected_hash=fs.read().hashes['human.md'])
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(memory.root), phase], capture_output=True, timeout=10
    )
    assert result.returncode == 0, result.stderr.decode()
    after = memory.read()
    assert after.blocks["human.md"] == (
        before.blocks["human.md"] if phase == "before" else "child committed"
    )
    fs = MemoryFS(memory.root)
    fs.update("human.md", "parent edit", expected_hash=after.hashes["human.md"])
