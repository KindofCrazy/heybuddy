import asyncio
from datetime import datetime

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
    tool = Tool("add", "Add", AddNumbersArguments, lambda args: args.a + args.b)
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
    registry.register(Tool("invalid", "invalid", ToolArguments, lambda _: value))
    result = await registry.execute(ToolCall(id="1", name="invalid", arguments="{}"))
    assert result.error.code == "execution_error"


async def test_tool_exception_not_exposed_in_result_or_log(caplog):
    def fail(_):
        raise ValueError("secret-test-token")

    registry = ToolRegistry()
    registry.register(Tool("fail", "fail", ToolArguments, fail))
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
    registry.register(Tool("broken", "broken", BrokenArguments, lambda _: None))
    result = await registry.execute(ToolCall(id="1", name="broken", arguments='{"value":"input"}'))
    assert result.error.code == "invalid_arguments"


async def test_pydantic_output_normalization():
    registry = ToolRegistry()
    registry.register(
        Tool("model", "model", ToolArguments, lambda _: AddNumbersArguments(a=1, b=2))
    )
    result = await registry.execute(ToolCall(id="1", name="model", arguments="{}"))
    assert result.ok
    assert result.data == {"a": 1.0, "b": 2.0}
