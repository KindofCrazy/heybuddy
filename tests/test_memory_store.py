import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from personal_agent.core.memory import CoreMemoryError
from personal_agent.memory.models import BLOCKS
from personal_agent.memory.store import MemoryStore


@pytest.fixture
def memory(tmp_path):
    store = MemoryStore.for_agent(tmp_path / "memory", "heybuddy")
    store.initialize()
    return store


def test_initialize_creates_versioned_markdown_and_is_idempotent(memory):
    original = memory.read()
    assert set(original.blocks) == set(BLOCKS)
    for name in BLOCKS:
        assert (
            memory.root / "versions" / original.revision.version / name
        ).read_text() == original.blocks[name]
    assert memory.initialize() == original
    assert len(memory.history()) == 1


def test_different_agents_do_not_share_memory_or_versions(tmp_path):
    first = MemoryStore.for_agent(tmp_path, "first")
    second = MemoryStore.for_agent(tmp_path, "second")
    initial = first.initialize()
    other = second.initialize()
    updated = first.update("human.md", "first user", expected_version=initial.revision.version)
    assert second.read() == other
    assert first.read() == updated
    with pytest.raises(CoreMemoryError) as caught:
        second.restore(updated.revision.version, expected_version=other.revision.version)
    assert caught.value.code == "unknown_version"


def test_update_reopen_and_restore_preserve_committed_history(memory):
    first = memory.read()
    second = memory.update(
        "human.md", "用户喜欢简洁中文。", expected_version=first.revision.version
    )
    reopened = MemoryStore(memory.root)
    assert reopened.read() == second
    restored = reopened.restore(first.revision.version, expected_version=second.revision.version)
    assert restored.blocks == first.blocks
    assert restored.revision.version not in {first.revision.version, second.revision.version}
    assert restored.revision.parent == second.revision.version
    assert restored.revision.restored_from == first.revision.version
    assert [item.version for item in reopened.history()] == [
        restored.revision.version,
        second.revision.version,
        first.revision.version,
    ]


def test_partial_edit_adds_c_while_preserving_a_b_and_other_blocks(memory):
    initial = memory.read()
    before = memory.update(
        "human.md", "# Human\n- A\n- B\n", expected_version=initial.revision.version
    )
    after = memory.edit("human.md", "- B\n", "- B\n- C\n", expected_version=before.revision.version)
    assert after.blocks["human.md"] == "# Human\n- A\n- B\n- C\n"
    assert after.blocks["persona.md"] == before.blocks["persona.md"]
    assert after.blocks["MEMORY.md"] == before.blocks["MEMORY.md"]
    assert after.revision.parent == before.revision.version
    assert MemoryStore(memory.root).read() == after


@pytest.mark.parametrize(
    "old_text,new_text,stale,code",
    [
        ("- B\n", "- B\n- C\n", True, "memory_conflict"),
        ("- missing\n", "- C\n", False, "memory_edit_mismatch"),
        ("- B ", "- C\n", False, "memory_edit_mismatch"),
        ("", "- C\n", False, "memory_edit_mismatch"),
        ("- B\n", "你" * 9000, False, "memory_too_large"),
    ],
    ids=["stale", "missing", "whitespace", "empty", "oversized"],
)
def test_rejected_partial_edits_leave_pointer_history_and_blocks_unchanged(
    memory, old_text, new_text, stale, code
):
    initial = memory.read()
    before = memory.update(
        "human.md", "# Human\n- A\n- B\n", expected_version=initial.revision.version
    )
    history = memory.history()
    pointer = (memory.root / "CURRENT").read_bytes()
    with pytest.raises(CoreMemoryError) as caught:
        memory.edit(
            "human.md",
            old_text,
            new_text,
            expected_version=initial.revision.version if stale else before.revision.version,
        )
    assert caught.value.code == code
    assert memory.read() == before
    assert memory.history() == history
    assert (memory.root / "CURRENT").read_bytes() == pointer


@pytest.mark.parametrize("content,anchor", [("A B B", "B"), ("aaa", "aa")])
def test_partial_edit_rejects_ambiguous_including_overlapping_matches(memory, content, anchor):
    before = memory.update("human.md", content, expected_version=memory.read().revision.version)
    with pytest.raises(CoreMemoryError) as caught:
        memory.edit("human.md", anchor, "C", expected_version=before.revision.version)
    assert caught.value.code == "memory_edit_mismatch"
    assert memory.read() == before


def test_partial_edit_can_correct_or_delete_only_a_matched_span(memory):
    before = memory.update("human.md", "A B C", expected_version=memory.read().revision.version)
    changed = memory.edit("human.md", "B", "D", expected_version=before.revision.version)
    deleted = memory.edit("human.md", " D", "", expected_version=changed.revision.version)
    assert changed.blocks["human.md"] == "A D C"
    assert deleted.blocks["human.md"] == "A C"


def test_partial_edit_publication_failure_keeps_previous_snapshot(memory, monkeypatch):
    before = memory.read()

    def fail(*args):
        raise OSError("private-path")

    monkeypatch.setattr("personal_agent.memory.store.os.replace", fail)
    with pytest.raises(CoreMemoryError) as caught:
        memory.edit("human.md", "# Human", "# Human\nC", expected_version=before.revision.version)
    assert caught.value.code == "memory_io_error"
    assert memory.read() == before
    assert len(memory.history()) == 1


@pytest.mark.parametrize("operation", ["update", "restore"])
def test_stale_versions_are_rejected_without_overwrite(memory, operation):
    stale = memory.read().revision.version
    other = MemoryStore(memory.root)
    current = other.update("human.md", "new", expected_version=stale)
    with pytest.raises(CoreMemoryError) as caught:
        if operation == "update":
            memory.update("human.md", "stale", expected_version=stale)
        else:
            memory.restore(stale, expected_version=stale)
    assert caught.value.code == "memory_conflict"
    assert memory.read() == current
    assert len(memory.history()) == 2


def test_concurrent_editors_cannot_lose_an_update(memory):
    expected = memory.read().revision.version
    ready = Barrier(2)

    def edit(text):
        store = MemoryStore(memory.root)
        ready.wait(timeout=3)
        try:
            return store.update("human.md", text, expected_version=expected).revision.version
        except CoreMemoryError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(edit, ["first", "second"]))
    failures = [item for item in outcomes if item in {"memory_busy", "memory_conflict"}]
    assert len(failures) == 1
    assert len(memory.history()) == 2
    assert memory.read().blocks["human.md"] in {"first", "second"}


@pytest.mark.parametrize("operation", ["update", "restore"])
def test_pointer_publication_failure_rolls_back_visible_snapshot(memory, monkeypatch, operation):
    first = memory.read()
    current = memory.update("human.md", "current", expected_version=first.revision.version)
    history = memory.history()

    def fail_replace(*args):
        raise OSError("private filesystem path")

    monkeypatch.setattr("personal_agent.memory.store.os.replace", fail_replace)
    with pytest.raises(CoreMemoryError) as caught:
        if operation == "update":
            memory.update("persona.md", "uncommitted", expected_version=current.revision.version)
        else:
            memory.restore(first.revision.version, expected_version=current.revision.version)
    assert caught.value.code == "memory_io_error"
    assert "private" not in str(caught.value)
    assert memory.read() == current
    assert memory.history() == history


def test_partial_snapshot_write_never_changes_current(memory, monkeypatch):
    before = memory.read()
    write = memory._write_file

    def fail_second(path, data):
        if path.name == "human.md":
            raise OSError("disk full")
        write(path, data)

    monkeypatch.setattr(memory, "_write_file", fail_second)
    with pytest.raises(CoreMemoryError):
        memory.update("persona.md", "unpublished", expected_version=before.revision.version)
    assert memory.read() == before
    assert len(memory.history()) == 1


def test_reader_observes_old_whole_snapshot_until_pointer_swap(memory, monkeypatch):
    before = memory.read()
    replace = os.replace
    observations = []

    def inspect_then_replace(source, destination):
        observations.append(MemoryStore(memory.root).read())
        replace(source, destination)

    monkeypatch.setattr("personal_agent.memory.store.os.replace", inspect_then_replace)
    after = memory.update("human.md", "new content", expected_version=before.revision.version)
    assert observations == [before]
    assert memory.read() == after


@pytest.mark.parametrize("content", ["你" * 200, "\n" * 400, '"' * 400, "\x00" * 100])
def test_limit_counts_compiled_utf8_including_json_escaping(tmp_path, content):
    memory = MemoryStore(tmp_path / "small", max_bytes=512)
    original = memory.initialize()
    assert len(content) <= 512
    with pytest.raises(CoreMemoryError) as caught:
        memory.update("human.md", content, expected_version=original.revision.version)
    assert caught.value.code == "memory_too_large"
    assert memory.read() == original


def test_exact_compiled_limit_and_one_byte_over(tmp_path):
    memory = MemoryStore(tmp_path / "boundary", max_bytes=512)
    initial = memory.initialize()
    encoded_value_size = (
        len(json.dumps(initial.blocks["human.md"], ensure_ascii=False).encode()) - 2
    )
    overhead = len(memory.compile().encode()) - encoded_value_size
    content = "x" * (512 - overhead)
    current = memory.update("human.md", content, expected_version=initial.revision.version)
    assert len(memory.compile().encode()) == 512
    with pytest.raises(CoreMemoryError, match="limit"):
        memory.update("human.md", content + "x", expected_version=current.revision.version)
    assert memory.read() == current


def test_all_blocks_share_one_budget(tmp_path):
    memory = MemoryStore(tmp_path / "total", max_bytes=512)
    current = memory.initialize()
    current = memory.update("human.md", "h" * 150, expected_version=current.revision.version)
    with pytest.raises(CoreMemoryError):
        memory.update("persona.md", "p" * 250, expected_version=current.revision.version)
    assert memory.read() == current


def test_smaller_budget_still_lists_history_and_rejects_oversized_restore(memory):
    initial = memory.read()
    large = memory.update("human.md", "x" * 1000, expected_version=initial.revision.version)
    current = memory.update("human.md", "small", expected_version=large.revision.version)
    small_store = MemoryStore(memory.root, max_bytes=512)
    assert small_store.read() == current
    assert len(small_store.history()) == 3
    with pytest.raises(CoreMemoryError) as caught:
        small_store.restore(large.revision.version, expected_version=current.revision.version)
    assert caught.value.code == "memory_too_large"
    assert small_store.read() == current


@pytest.mark.parametrize(
    "block", ["../.env", "/tmp/outside.md", "base_system_prompt", "tools.json", "persona.md/../x"]
)
def test_only_fixed_block_names_can_be_updated(memory, block):
    before = memory.read()
    with pytest.raises(CoreMemoryError) as caught:
        memory.update(block, "overwrite", expected_version=before.revision.version)
    assert caught.value.code == "invalid_block"
    assert memory.read() == before


@pytest.mark.parametrize("version", ["../outside", "a" * 31, "A" * 32, "z" * 32])
def test_restore_cannot_traverse_paths(memory, version):
    before = memory.read()
    with pytest.raises(CoreMemoryError):
        memory.restore(version, expected_version=before.revision.version)
    assert memory.read() == before


def test_unpublished_or_foreign_revisions_cannot_be_restored(memory, monkeypatch, tmp_path):
    before = memory.read()
    with monkeypatch.context() as patch:
        patch.setattr(
            "personal_agent.memory.store.os.replace", lambda *args: (_ for _ in ()).throw(OSError())
        )
        with pytest.raises(CoreMemoryError):
            memory.update("human.md", "orphan", expected_version=before.revision.version)
    orphan = next(
        path.name
        for path in (memory.root / "versions").iterdir()
        if path.name != before.revision.version
    )
    other = MemoryStore.for_agent(tmp_path / "other", "another")
    foreign = other.initialize().revision.version
    for version in [orphan, foreign]:
        with pytest.raises(CoreMemoryError) as caught:
            memory.restore(version, expected_version=before.revision.version)
        assert caught.value.code == "unknown_version"
    assert memory.read() == before


def test_modified_markdown_fails_integrity_check_without_being_injected(memory):
    current = memory.read()
    path = memory.root / "versions" / current.revision.version / "human.md"
    path.write_text("private tampered instructions", encoding="utf-8")
    with pytest.raises(CoreMemoryError) as caught:
        memory.compile()
    assert caught.value.code == "invalid_memory"
    assert "private" not in str(caught.value)


def test_invalid_current_pointer_never_resets_existing_memory(memory):
    pointer = memory.root / "CURRENT"
    pointer.write_text("invalid-private-pointer", encoding="utf-8")
    with pytest.raises(CoreMemoryError):
        memory.initialize()
    assert pointer.read_text() == "invalid-private-pointer"


def test_missing_pointer_with_existing_versions_is_not_reinitialized(memory):
    (memory.root / "CURRENT").unlink()
    with pytest.raises(CoreMemoryError, match="will not be reset"):
        memory.initialize()


def test_memory_rejects_symlinked_pointer(memory, tmp_path):
    pointer = memory.root / "CURRENT"
    destination = tmp_path / "outside"
    destination.write_bytes(pointer.read_bytes())
    pointer.unlink()
    try:
        pointer.symlink_to(destination)
    except OSError:
        pytest.skip("OS does not allow creating symlinks")
    with pytest.raises(CoreMemoryError, match="links"):
        memory.read()


def test_agent_id_cannot_escape_memory_root(tmp_path):
    with pytest.raises(CoreMemoryError):
        MemoryStore.for_agent(tmp_path, "../other")


@pytest.mark.parametrize("phase", ["before", "after"])
def test_process_exit_at_publication_boundary_never_exposes_partial_snapshot(memory, phase):
    before = memory.read()
    script = """
import os,sys
from pathlib import Path
from personal_agent.memory.store import MemoryStore
store=MemoryStore(Path(sys.argv[1]))
replace=os.replace
def crash_at_publication(source,destination):
    if sys.argv[2]=='after':
        replace(source,destination)
    os._exit(0)
os.replace=crash_at_publication
store.update('human.md','child committed',expected_version=store.read().revision.version)
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(memory.root), phase], capture_output=True, timeout=10
    )
    assert process.returncode == 0, process.stderr.decode()
    after = memory.read()
    if phase == "before":
        assert after == before
        assert len(memory.history()) == 1
    else:
        assert after.blocks["human.md"] == "child committed"
        assert after.blocks["persona.md"] == before.blocks["persona.md"]
        assert after.blocks["MEMORY.md"] == before.blocks["MEMORY.md"]
        assert len(memory.history()) == 2
    # The child's writer lock must also have been released by the OS.
    memory.update("human.md", "parent edit", expected_version=after.revision.version)
