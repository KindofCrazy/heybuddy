import asyncio
from datetime import datetime
from threading import Event

import pytest
from pydantic import field_validator

from personal_agent.core.models import ToolCall
from personal_agent.tools.builtin import AddNumbersArguments, create_builtin_registry
from personal_agent.tools.registry import Tool, ToolArguments, ToolRegistry
from tests.fakes import add_call


async def test_builtin_time_is_aware_utc():
    result = await create_builtin_registry().execute(
        ToolCall(id="clock", name="get_current_time", arguments="{}")
    )
    assert result.ok
    parsed = datetime.fromisoformat(result.data["time"])
    assert parsed.utcoffset().total_seconds() == 0
    assert result.data["timezone"] == "UTC"


@pytest.mark.parametrize(
    "arguments",
    [
        "[]",
        "null",
        "42",
        "{}",
        '{"a": 1}',
        '{"a": true, "b": 2}',
        '{"a": "1", "b": 2}',
        '{"a": 1, "b": 2, "extra": 3}',
        '{"a": NaN, "b": 2}',
        '{"a": Infinity, "b": 2}',
    ],
)
async def test_invalid_arguments_rejected(arguments):
    result = await create_builtin_registry().execute(add_call(arguments=arguments))
    assert not result.ok
    assert result.error.code == "invalid_arguments"


async def test_unknown_tool_rejected():
    result = await ToolRegistry().execute(ToolCall(id="1", name="shell", arguments="{}"))
    assert result.error.code == "unknown_tool"
    assert result.call_id == "1"


def test_registry_rejects_duplicates_and_bad_names():
    registry = ToolRegistry()
    tool = Tool(
        "add", "Add", AddNumbersArguments, lambda args: args.a + args.b, side_effect_free=True
    )
    registry.register(tool)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(tool)
    with pytest.raises(ValueError, match="name"):
        registry.register(Tool("bad name", "bad", ToolArguments, lambda _: None))
    definition = registry.definitions()[0]
    assert definition.parameters["additionalProperties"] is False
    assert definition.parameters["required"] == ["a", "b"]


async def test_async_tool_timeout():
    async def slow(_):
        await asyncio.Event().wait()

    registry = ToolRegistry(timeout_seconds=0.01)
    registry.register(Tool("slow", "slow", ToolArguments, slow))
    result = await asyncio.wait_for(
        registry.execute(ToolCall(id="slow-1", name="slow", arguments="{}")), timeout=1
    )
    assert result.error.code == "tool_timeout"


@pytest.mark.parametrize("value", [object(), {"bad": object()}, float("inf"), float("nan")])
async def test_unserializable_or_nonfinite_results_are_errors(value):
    registry = ToolRegistry()
    registry.register(
        Tool("invalid", "invalid", ToolArguments, lambda _: value, side_effect_free=True)
    )
    result = await registry.execute(ToolCall(id="1", name="invalid", arguments="{}"))
    assert result.error.code == "execution_error"


async def test_tool_exception_not_exposed_in_result_or_log(caplog):
    def fail(_):
        raise ValueError("secret-test-token")

    registry = ToolRegistry()
    registry.register(Tool("fail", "fail", ToolArguments, fail, side_effect_free=True))
    result = await registry.execute(ToolCall(id="1", name="fail", arguments="{}"))
    assert result.error.code == "execution_error"
    assert "secret-test-token" not in result.model_dump_json() + caplog.text


async def test_custom_validator_bug_becomes_tool_error():
    class BrokenArguments(ToolArguments):
        value: str

        @field_validator("value")
        @classmethod
        def broken(cls, value):
            raise RuntimeError("private validator failure")

    registry = ToolRegistry()
    registry.register(
        Tool("broken", "broken", BrokenArguments, lambda _: None, side_effect_free=True)
    )
    result = await registry.execute(ToolCall(id="1", name="broken", arguments='{"value":"input"}'))
    assert result.error.code == "invalid_arguments"


async def test_pydantic_output_normalization():
    registry = ToolRegistry()
    registry.register(
        Tool(
            "model",
            "model",
            ToolArguments,
            lambda _: AddNumbersArguments(a=1, b=2),
            side_effect_free=True,
        )
    )
    result = await registry.execute(ToolCall(id="1", name="model", arguments="{}"))
    assert result.ok
    assert result.data == {"a": 1.0, "b": 2.0}


def test_sync_tools_require_explicit_side_effect_free_contract():
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="side_effect_free=True"):
        registry.register(Tool("unsafe", "No safety declaration", ToolArguments, lambda _: None))
    assert registry.definitions() == []


async def test_sync_timeout_reports_unknown_completion_and_thread_can_finish_later():
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    finished = asyncio.Event()
    release = Event()

    def blocked_read(_):
        # Events are test instrumentation; the tool performs no external writes.
        loop.call_soon_threadsafe(started.set)
        release.wait(timeout=5)
        loop.call_soon_threadsafe(finished.set)
        return "read complete"

    registry = ToolRegistry(timeout_seconds=0.1)
    registry.register(Tool("read", "Read only", ToolArguments, blocked_read, side_effect_free=True))
    task = asyncio.create_task(
        registry.execute(ToolCall(id="thread-1", name="read", arguments="{}"))
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        result = await asyncio.wait_for(task, timeout=2)
        assert not result.ok
        assert result.call_id == "thread-1"
        assert result.error.code == "tool_timeout"
        assert "thread may still run" in result.error.message
        assert "Completion is unknown" in result.error.message
        assert "not cancelled or rolled back" in result.error.message
        assert "do not automatically retry" in result.error.message
        assert not finished.is_set()
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=2)
        await task
    # A late completion must not turn the earlier timeout result into success.
    assert not result.ok


@pytest.mark.parametrize(
    "payload", [{"edited": True, "complete": False}, {"value": object()}, {"value": float("nan")}]
)
async def test_sanitized_tool_failure_data_uses_json_contract(payload, caplog):
    from personal_agent.tools.registry import ToolExecutionError

    async def fail(_):
        raise ToolExecutionError("partial", "Partial operation; no retry.", data=payload)

    tools = ToolRegistry()
    tools.register(Tool("partial", "partial", ToolArguments, fail))
    result = await tools.execute(ToolCall(id="partial-call", name="partial", arguments="{}"))
    assert not result.ok and result.call_id == "partial-call"
    if "edited" in payload:
        assert result.error.code == "partial" and result.data == payload
    else:
        assert result.error.code == "execution_error" and result.data is None
        assert "value" not in result.error.message + caplog.text
