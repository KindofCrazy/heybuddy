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
    assert "BBC" in package.character and "不继承" in package.character
    assert package.profile is not None and "说话方式" in package.profile
    assert "感兴趣时" in package.behavior and len(package.examples) == 2
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
    path = packages / "sherlock_bbc" / "soul.md"
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
            stream.write("\n# 身份\nDuplicate")
    with pytest.raises(CharacterError):
        CharacterLoader(packages).load("sherlock_bbc")


def test_changed_markdown_changes_package_fingerprint(packages):
    loader = CharacterLoader(packages)
    before = loader.load("sherlock_bbc")
    path = packages / "sherlock_bbc" / "soul.md"
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\nA host-edited illustration.\n")
    assert loader.load("sherlock_bbc").fingerprint != before.fingerprint


def test_legacy_four_file_packages_remain_readable(tmp_path):
    folder = tmp_path / "legacy"
    folder.mkdir()
    (folder / "character.md").write_text(
        "## Identity\n旧角色\n## Core Personality\n好奇\n"
        "## Flaws & Contradictions\n急躁\n## Character Adaptation\n遵守宿主\n",
        encoding="utf-8",
    )
    for name in ("behavior.md", "examples.md", "lore.md"):
        (folder / name).write_text("## 闲聊\n自然接话\n## 认真\n核实事实\n", encoding="utf-8")
    package = CharacterLoader(tmp_path).load("legacy")
    assert package.profile is None and "旧角色" in package.character
    # A present but broken soul must fail, rather than silently use old identity files.
    (folder / "soul.md").write_text("# 身份\n不完整", encoding="utf-8")
    with pytest.raises(CharacterError, match="character_sections_missing"):
        CharacterLoader(tmp_path).load("legacy")


def test_soul_is_authoritative_and_shared_by_chat_and_state(packages):
    folder = packages / "sherlock_bbc"
    (folder / "character.md").write_text("OBSOLETE_IDENTITY", encoding="utf-8")
    package = CharacterLoader(packages).load("sherlock_bbc")
    assert "OBSOLETE_IDENTITY" not in package.character
    assert package.profile["情绪反应"] == package.behavior
    assert package.profile["存在方式"] in package.character
    before = package.fingerprint
    (folder / "character.md").write_text("OTHER_OBSOLETE_IDENTITY", encoding="utf-8")
    assert CharacterLoader(packages).load("sherlock_bbc").fingerprint == before


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
