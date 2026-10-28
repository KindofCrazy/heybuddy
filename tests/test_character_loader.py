import shutil

import pytest

from personal_agent.character.loader import PACKAGE_ROOT, CharacterLoader
from personal_agent.character.models import CharacterError, CharacterPolicy


@pytest.fixture
def packages(tmp_path):
    root = tmp_path / "characters"
    shutil.copytree(PACKAGE_ROOT, root)
    return root


def test_bbc_package_is_complete_original_and_has_stable_identity():
    package = CharacterLoader().load("sherlock_bbc")
    assert package.character_id == "sherlock_bbc"
    assert "BBC" in package.character and "不是 John" in package.character
    assert "Character Adaptation" in package.character
    assert "情境：" in package.behavior and len(package.examples) >= 2
    assert len(package.lore) >= 4 and len(package.fingerprint) == 64
    assert CharacterLoader().load("sherlock_bbc") == package


@pytest.mark.parametrize(
    "name",
    [
        "../sherlock_bbc",
        "..\\sherlock_bbc",
        "/absolute",
        "C:/escape",
        "SHERLOCK",
        "sherlock_bbc/x",
        "none",
        "missing",
    ],
)
def test_invalid_or_missing_character_is_explicit_error(name):
    with pytest.raises(CharacterError):
        CharacterLoader().load(name)


@pytest.mark.parametrize(
    "mode", ["missing", "encoding", "empty", "oversized", "sections", "duplicate"]
)
def test_invalid_package_never_returns_partial_content(packages, mode):
    path = packages / "sherlock_bbc" / "character.md"
    if mode == "missing":
        path.unlink()
    elif mode == "encoding":
        path.write_bytes(b"\xff\xfe\xff")
    elif mode == "empty":
        path.write_text(" ", encoding="utf-8")
    elif mode == "oversized":
        path.write_bytes(b"a" * 16385)
    elif mode == "sections":
        path.write_text("# Identity only", encoding="utf-8")
    else:
        with path.open("a", encoding="utf-8") as stream:
            stream.write("\n## Identity\nDuplicate")
    with pytest.raises(CharacterError):
        CharacterLoader(packages).load("sherlock_bbc")


def test_changed_markdown_changes_package_fingerprint(packages):
    loader = CharacterLoader(packages)
    before = loader.load("sherlock_bbc")
    path = packages / "sherlock_bbc" / "behavior.md"
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\nA host-edited illustration.\n")
    assert loader.load("sherlock_bbc").fingerprint != before.fingerprint


def test_file_budget_is_host_controlled(packages):
    with pytest.raises(CharacterError, match="package_file_budget"):
        CharacterLoader(packages, CharacterPolicy(max_file_bytes=1024)).load("sherlock_bbc")


def test_linked_package_or_file_is_rejected(packages, tmp_path):
    outside = tmp_path / "outside"
    shutil.copytree(packages / "sherlock_bbc", outside)
    linked = packages / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("OS does not allow creating symlinks")
    with pytest.raises(CharacterError, match="linked_package_path"):
        CharacterLoader(packages).load("linked")


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows junction contract")
def test_junction_escape_is_rejected(packages, tmp_path):
    import subprocess

    outside = tmp_path / "outside"
    shutil.copytree(packages / "sherlock_bbc", outside)
    linked = packages / "linked"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(linked), str(outside)], capture_output=True, check=True
    )
    with pytest.raises(CharacterError, match="linked_package_path"):
        CharacterLoader(packages).load("linked")
