import hashlib
import json
import shutil

import pytest

from personal_agent.character.context import CharacterContextCompiler
from personal_agent.character.loader import PACKAGE_ROOT, CharacterLoader
from personal_agent.character.models import CharacterError, CharacterPolicy
from personal_agent.core.context import ContextBuilder
from personal_agent.core.prompts import CONVERSATION_PROMPT


def minimal_persona():
    return {
        "soul": {
            "identity": {"personaName": "Luna", "slug": "luna", "bio": "喜欢音乐和代码"},
            "character": {"personality": "温柔，好奇", "speakingStyle": "自然接话"},
        }
    }


def save_persona(root, document):
    folder = root / "luna"
    folder.mkdir(exist_ok=True)
    (folder / "persona.json").write_text(json.dumps(document), encoding="utf-8")
    return folder


def test_minimum_openpersona_needs_no_markdown_sections_examples_or_lore(tmp_path):
    save_persona(tmp_path, minimal_persona())
    package = CharacterLoader(tmp_path).load("luna")
    assert package.profile is None
    assert package.persona["姓名"] == "Luna"
    assert package.examples == package.lore == []
    compiler = CharacterContextCompiler(package)
    context = ContextBuilder(character=compiler)
    messages = context.for_request(context.build("你好"))
    assert messages[0].text.startswith("你是 Luna。")
    assert "喜欢音乐和代码" in messages[0].text
    assert messages[0].text.count(CONVERSATION_PROMPT) == 1
    assert (
        compiler.diagnostics()["system_hash"]
        == hashlib.sha256(messages[0].text.encode()).hexdigest()
    )


def test_bundled_luna_resolves_local_guide_and_is_independent_of_legacy_role_features():
    package = CharacterLoader().load("luna")
    assert "Juilliard" in package.persona["背景"]
    assert "不急着替他解决" in package.persona["行为指南"]

    def unexpected(*args):
        pytest.fail("JSON persona must not fetch lore, role provenance or transient state")

    prompts = []
    for variant in ("E0", "E1", "E2"):
        compiler = CharacterContextCompiler(
            package, variant=variant, state=unexpected, source_scope=unexpected
        )
        for text in ("你好", "John Watson 与贝克街", "你记得我吗"):
            messages = compiler.compile(text, source_run_ids=("synthetic-run",))
            assert len(messages) == 1 and messages[0].role == "system"
            assert compiler.diagnostics()["state_available"] is False
            prompts.append(messages[0].text)
    assert len(set(prompts)) == 1
    prompt = prompts[0]
    assert "file:soul" not in prompt
    assert "Sherlock" not in prompt and "Miru" not in prompt
    for declaration in ("openclaw", "elevenlabs", "Bash(", "a2a", "heartbeat", "railway"):
        assert declaration not in prompt


def test_unused_runtime_permissions_and_unknown_soul_fields_never_enter_prompt(tmp_path):
    document = minimal_persona()
    document.update(
        body={"runtime": {"framework": "INERT_BODY"}},
        additionalAllowedTools=["INERT_BASH"],
        rhythm={"heartbeat": {"enabled": True, "strategy": "INERT_HEARTBEAT"}},
        evolution={"enabled": True},
    )
    document["soul"]["unknown"] = "INERT_SOUL_EXTENSION"
    save_persona(tmp_path, document)
    prompt = CharacterContextCompiler(CharacterLoader(tmp_path).load("luna")).compile("你好")[0]
    assert "INERT_" not in prompt.text


def test_full_guide_is_inlined_once_and_chat_style_remains_the_final_default():
    package = CharacterLoader().load("luna")
    prompt = CharacterContextCompiler(package).compile("你好")[0].text
    assert prompt.startswith("你是 Luna，一位22岁的AI 女友。")
    assert "陪伴者" in prompt and "💕" in prompt
    assert package.persona["背景"] in prompt
    assert prompt.count(package.persona["行为指南"]) == 1
    assert prompt.endswith(CONVERSATION_PROMPT)
    for heading in ("【年龄】", "【类型】", "【角色】", "【表情】", "【氛围】"):
        assert heading not in prompt


@pytest.mark.parametrize(
    "mode",
    ["invalid_json", "duplicate", "missing", "empty", "type", "constant", "root", "escaped_nul"],
)
def test_malformed_json_is_safe_error_and_never_falls_back_to_soul(tmp_path, mode):
    document = minimal_persona()
    if mode == "missing":
        del document["soul"]["identity"]["personaName"]
    elif mode == "empty":
        document["soul"]["character"]["personality"] = " "
    elif mode == "type":
        document["soul"]["identity"]["personaName"] = {"private": "PRIVATE_INPUT"}
    elif mode == "escaped_nul":
        document["soul"]["identity"]["personaName"] = "Luna\x00"
    elif mode == "root":
        document = []
    folder = save_persona(tmp_path, document)
    (folder / "soul.md").write_text("OLD_IDENTITY", encoding="utf-8")
    if mode in {"invalid_json", "duplicate", "constant"}:
        raw = {
            "invalid_json": '{"PRIVATE_INPUT":',
            "duplicate": '{"soul": {}, "soul": {"PRIVATE_INPUT": "x"}}',
            "constant": '{"soul": NaN}',
        }[mode]
        (folder / "persona.json").write_text(raw, encoding="utf-8")
    with pytest.raises(CharacterError, match="invalid_persona_json") as error:
        CharacterLoader(tmp_path).load("luna")
    assert "PRIVATE_INPUT" not in str(error.value)


@pytest.mark.parametrize(
    "reference",
    ["file:../../outside.md", "file:C:/outside.md", "file:/outside.md", "https://example.com/x"],
)
def test_guide_does_not_follow_arbitrary_files_or_urls(tmp_path, reference):
    document = minimal_persona()
    document["soul"]["character"]["behaviorGuide"] = reference
    save_persona(tmp_path, document)
    with pytest.raises(CharacterError, match="invalid_persona_guide"):
        CharacterLoader(tmp_path).load("luna")


def test_persona_and_guide_both_affect_fingerprint_and_obey_file_and_context_limits(tmp_path):
    folder = tmp_path / "luna"
    shutil.copytree(PACKAGE_ROOT / "luna", folder)
    loader = CharacterLoader(tmp_path)
    before = loader.load("luna")
    guide = folder / "soul/behavior-guide.md"
    guide.write_text(guide.read_text(encoding="utf-8") + "\n更喜欢安静。", encoding="utf-8")
    assert loader.load("luna").fingerprint != before.fingerprint
    with pytest.raises(CharacterError, match="character_base_budget"):
        CharacterContextCompiler(before, CharacterPolicy(max_context_bytes=1024))
    with pytest.raises(CharacterError, match="package_file_budget"):
        CharacterLoader(tmp_path, CharacterPolicy(max_file_bytes=1024)).load("luna")
    guide.write_bytes(b"x" * 16385)
    with pytest.raises(CharacterError, match="package_file_budget"):
        loader.load("luna")


def test_linked_persona_guide_is_rejected(tmp_path):
    document = minimal_persona()
    document["soul"]["character"]["behaviorGuide"] = "file:soul/behavior-guide.md"
    folder = save_persona(tmp_path, document)
    (folder / "soul").mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("PRIVATE_OUTSIDE", encoding="utf-8")
    try:
        (folder / "soul/behavior-guide.md").symlink_to(outside)
    except OSError:
        pytest.skip("OS does not allow creating symlinks")
    with pytest.raises(CharacterError, match="linked_package_path"):
        CharacterLoader(tmp_path).load("luna")
