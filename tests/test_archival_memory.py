"""The unified read-only tool preserves bounds, hashes and MemoryFS isolation."""

import json

import pytest

from personal_agent.core.agent import Agent
from personal_agent.core.context import ContextBuilder
from personal_agent.core.models import ModelResponse, ToolCall, ToolMessage
from personal_agent.memory import index
from personal_agent.memory.models import OUTPUT_BYTES
from personal_agent.memory.store import MemoryFS
from personal_agent.memory.tools import register_memory_tools
from personal_agent.tools.registry import ToolRegistry
from tests.fakes import FakeProvider


@pytest.fixture
def archive(tmp_path):
    fs = MemoryFS.for_agent(tmp_path / "memory", "heybuddy")
    fs.initialize()
    registry = ToolRegistry()
    register_memory_tools(registry, fs, unified_reads=True)
    return fs, registry


async def call(archive, **arguments):
    return await archive[1].execute(
        ToolCall(id="archive", name="archival_memory_search", arguments=json.dumps(arguments))
    )


def topic(body):
    return "---\nname: 文档\ndescription: 整理后的资料\n---\n\n# 资料\n" + body


async def test_body_search_then_hash_checked_read_and_conflict(archive):
    fs, registry = archive
    text = topic("前文。" * 250 + "unique_body_marker 记录正文事实。\n" + "后文。" * 150)
    index.create(fs, "topics/notes.md", text)
    before = fs.read()
    result = await call(archive, query="unique_body_marker")
    assert result.ok and result.data["trust"] == "data_only"
    hit = result.data["results"][0]
    assert hit["path"] == "topics/notes.md" and "unique_body_marker" in hit["content"]
    page = await call(
        archive, path=hit["path"], expected_hash=hit["hash"], offset=hit["offset"], limit=256
    )
    assert page.ok and page.data["truncated"]
    assert len(json.dumps(result.data, ensure_ascii=False).encode()) <= OUTPUT_BYTES
    index.edit(fs, hit["path"], "正文事实", "更新后的事实", expected_hash=hit["hash"])
    conflict = await call(archive, path=hit["path"], expected_hash=hit["hash"])
    assert conflict.error.code == "memory_conflict"
    assert {d.name for d in registry.definitions()} == {"archival_memory_search"}
    assert fs.read().blocks["human.md"] == before.blocks["human.md"]
    assert fs.read().blocks["persona.md"] == before.blocks["persona.md"]


async def test_index_directory_and_inspection_share_one_read_only_entry(archive):
    fs, _ = archive
    index.create(fs, "topics/one.md", topic("第一份资料"))
    index.create(fs, "topics/two.md", topic("第二份资料"))
    before = fs.read()
    root = await call(archive, query="index")
    alias = await call(archive, path="index")
    assert root.ok and alias.data == root.data
    assert "topics/MEMORY.md" in root.data["content"]
    directory = await call(archive, directory="topics", max_results=1)
    assert directory.ok and directory.data["next_offset"] is not None
    check = await call(archive, check_index=True)
    assert check.ok and check.data["ok"]
    assert fs.read() == before


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query": "x", "path": "index"},
        {"query": " "},
        {"directory": "", "check_index": True},
        {"query": "x", "offset": 1},
        {"directory": "", "expected_hash": "0" * 64},
        {"query": "x", "max_results": 99},
        {"path": "index", "write": True},
    ],
)
async def test_invalid_or_ambiguous_operations_are_rejected(archive, arguments):
    result = await call(archive, **arguments)
    assert not result.ok and result.error.code == "invalid_arguments"


@pytest.mark.parametrize("path", ["../other/human.md", "human.md", "persona.md", "C:/secret.md"])
async def test_paths_cannot_bypass_core_or_agent_boundary(archive, path):
    result = await call(archive, path=path)
    assert not result.ok and result.error.code == "invalid_path"


async def test_body_scan_reports_partial_scope_instead_of_claiming_no_memory(archive):
    fs, _ = archive
    for number in range(17):
        index.create(
            fs,
            f"topics/file{number:02}.md",
            topic("hidden_body_marker" if number == 16 else "无关正文"),
        )
    result = await call(archive, query="hidden_body_marker")
    assert result.ok and result.data["scope_limited"]
    assert result.data["scanned_files"] == 16 and not result.data["results"]


async def test_unified_tool_result_reaches_next_agent_round_with_call_id(archive):
    fs, registry = archive
    index.create(fs, "topics/pets.md", topic("用户家有一只猫。"))
    provider = FakeProvider(
        ModelResponse(
            tool_calls=[
                ToolCall(
                    id="pet-search",
                    name="archival_memory_search",
                    arguments=json.dumps({"query": "猫"}),
                )
            ]
        ),
        ModelResponse(text="记录中提到了你家的猫。"),
    )
    result = await Agent(provider, registry, context=ContextBuilder(memory=fs)).run(
        "查一下宠物记录"
    )
    assert result.status == "completed"
    tool = next(m for m in provider.requests[1] if isinstance(m, ToolMessage))
    assert tool.result.call_id == "pet-search" and tool.result.ok
    assert "猫" in tool.result.data["results"][0]["content"]
