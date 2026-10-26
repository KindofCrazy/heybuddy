"""Synthetic V0.4 fixtures use the original revision schema and byte hashes."""

import json
from pathlib import Path

import pytest

from personal_agent.config import Settings, memory_root_for
from personal_agent.core.memory import CoreMemoryError
from personal_agent.memory.models import BLOCKS, digest
from personal_agent.memory.store import MemoryFS


@pytest.fixture
def legacy(tmp_path):
    root = tmp_path / "memory" / "heybuddy"
    version = "a" * 32
    current = root / "versions" / version
    current.mkdir(parents=True)
    raws = {
        "persona.md": "# 原身份\r\n原文不变。\r\n".encode(),
        "human.md": "---\n用户旧笔记\n---\n不添加 frontmatter\n".encode(),
        "MEMORY.md": b"# Memory\n\nexact old text\n",
    }
    for name, raw in raws.items():
        (current / name).write_bytes(raw)
    (current / "revision.json").write_text(
        json.dumps(
            {
                "version": version,
                "parent": None,
                "created_at": "2026-01-01T00:00:00Z",
                "action": "initialize",
                "restored_from": None,
                "hashes": {name: digest(raw) for name, raw in raws.items()},
            }
        ),
        encoding="utf-8",
    )
    (root / "CURRENT").write_bytes((version + "\n").encode())
    orphan = root / "versions" / ("b" * 32)
    orphan.mkdir()
    (orphan / "uncommitted.md").write_bytes(b"orphan retained")
    return MemoryFS(root), raws


def test_legacy_export_keeps_every_byte_and_all_old_history(legacy):
    fs, raws = legacy
    data = fs.initialize()
    assert {name: text.encode() for name, text in data.blocks.items()} == raws
    for name, raw in raws.items():
        assert (fs.root / name).read_bytes() == raw
    backups = list(fs.root.glob(".migration-backup-v04-*"))
    assert len(backups) == 1
    backup = backups[0]
    assert (backup / "CURRENT").read_bytes() == (fs.root / "CURRENT").read_bytes()
    assert (backup / "versions" / ("b" * 32) / "uncommitted.md").read_bytes() == b"orphan retained"
    for name, raw in raws.items():
        assert (backup / "versions" / ("a" * 32) / name).read_bytes() == raw
    (fs.root / "human.md").write_bytes(b"editor update after migration")
    # After migration the old pointer is not consulted at all.
    (fs.root / "CURRENT").write_bytes(b"now invalid")
    assert MemoryFS(fs.root).initialize().blocks["human.md"] == "editor update after migration"
    assert list(fs.root.glob(".migration-backup-v04-*")) == backups


@pytest.mark.parametrize("failure", ["backup", "first", "second", "receipt_complete"])
def test_migration_failure_is_repeatable_and_never_destroys_source(legacy, monkeypatch, failure):
    fs, raws = legacy
    original = fs.atomic
    before = {p.relative_to(fs.root): p.read_bytes() for p in fs.root.rglob("*") if p.is_file()}

    def interrupt(path, raw, *, expected_hash):
        stop = (
            (failure == "backup" and path.name == ".backup-complete")
            or (failure == "first" and path == fs.root / "persona.md")
            or (failure == "second" and path == fs.root / "human.md")
            or (failure == "receipt_complete" and path.name == ".migration-v05" and b"true" in raw)
        )
        if stop:
            raise OSError("private failure")
        return original(path, raw, expected_hash=expected_hash)

    with monkeypatch.context() as patch:
        patch.setattr(fs, "atomic", interrupt)
        with pytest.raises(CoreMemoryError):
            fs.initialize()
    for relative, raw in before.items():
        assert (fs.root / relative).read_bytes() == raw
    data = MemoryFS(fs.root).initialize()
    assert {name: text.encode() for name, text in data.blocks.items()} == raws
    for name, raw in raws.items():
        assert (fs.root / name).read_bytes() == raw
    assert json.loads((fs.root / ".migration-v05").read_text())["complete"]


def test_interrupted_migration_preserves_external_editor_changes(legacy, monkeypatch):
    fs, _ = legacy
    original = fs.atomic

    def stop(path, raw, **kwargs):
        if path == fs.root / "human.md":
            raise OSError()
        return original(path, raw, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(fs, "atomic", stop)
        with pytest.raises(CoreMemoryError):
            fs.initialize()
    (fs.root / "persona.md").write_bytes(b"external editor change")
    with pytest.raises(CoreMemoryError) as caught:
        MemoryFS(fs.root).initialize()
    assert caught.value.code == "migration_conflict"
    assert (fs.root / "persona.md").read_bytes() == b"external editor change"
    assert not (fs.root / "human.md").exists()


def test_new_files_beside_legacy_are_never_silently_overwritten(legacy):
    fs, _ = legacy
    (fs.root / "human.md").write_bytes(b"new-format user edits")
    with pytest.raises(CoreMemoryError) as caught:
        fs.initialize()
    assert caught.value.code == "migration_conflict"
    assert (fs.root / "human.md").read_bytes() == b"new-format user edits"
    assert not (fs.root / "persona.md").exists()


@pytest.mark.parametrize("corruption", ["hash", "pointer", "missing_pointer", "missing_block"])
def test_corrupt_legacy_never_exports_or_resets(legacy, corruption):
    fs, _ = legacy
    directory = fs.root / "versions" / ("a" * 32)
    if corruption == "hash":
        (directory / "human.md").write_bytes(b"tampered")
    elif corruption == "pointer":
        (fs.root / "CURRENT").write_bytes(b"../foreign")
    elif corruption == "missing_pointer":
        (fs.root / "CURRENT").unlink()
    else:
        (directory / "human.md").unlink()
    with pytest.raises(CoreMemoryError):
        fs.initialize()
    assert not any((fs.root / name).exists() for name in BLOCKS)
    assert (fs.root / "versions").is_dir()


def test_incomplete_new_format_is_not_reset(tmp_path):
    fs = MemoryFS(tmp_path / "new")
    fs.root.mkdir()
    (fs.root / "human.md").write_bytes(b"do not erase")
    with pytest.raises(CoreMemoryError) as caught:
        fs.initialize()
    assert caught.value.code == "incomplete_memory"
    assert (fs.root / "human.md").read_bytes() == b"do not erase"


def test_legacy_default_location_takes_precedence_and_explicit_root_wins(tmp_path, monkeypatch):
    settings = Settings(
        context_window=64000,
        api_key="test-key",
        model="test-model",
        database_path=tmp_path / "history.sqlite3",
    )
    monkeypatch.setattr("personal_agent.config.default_memory_root", lambda: tmp_path / "user-data")
    assert memory_root_for(settings, "heybuddy") == tmp_path / "user-data"
    adjacent = Path(str(settings.database_path) + ".memory")
    (adjacent / "heybuddy").mkdir(parents=True)
    assert memory_root_for(settings, "heybuddy") == adjacent
    explicit = settings.model_copy(update={"memory_root": tmp_path / "explicit"})
    assert memory_root_for(explicit, "heybuddy") == tmp_path / "explicit"


def test_legacy_at_old_compiled_capacity_still_migrates_losslessly(legacy):
    fs, raws = legacy
    old_blocks = {name: raw.decode() for name, raw in raws.items()}
    old_blocks["human.md"] = ""
    original_payload = {
        "kind": "agent_core_memory",
        "trust": "data_only",
        "version": "a" * 32,
        "blocks": old_blocks,
    }
    overhead = len(json.dumps(original_payload, ensure_ascii=False).encode())
    old_blocks["human.md"] = "x" * (8192 - overhead)
    assert len(json.dumps(original_payload, ensure_ascii=False).encode()) == 8192
    directory = fs.root / "versions" / ("a" * 32)
    raw = old_blocks["human.md"].encode()
    (directory / "human.md").write_bytes(raw)
    revision_path = directory / "revision.json"
    revision = json.loads(revision_path.read_text(encoding="utf-8"))
    revision["hashes"]["human.md"] = digest(raw)
    revision_path.write_text(json.dumps(revision), encoding="utf-8")
    assert fs.initialize().blocks == old_blocks
    assert len(fs.compile().encode()) <= 8192
