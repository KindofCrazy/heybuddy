import asyncio
import logging

import pytest

from personal_agent.core.agent import Agent
from personal_agent.core.context import ContextBuilder
from personal_agent.core.models import AssistantMessage, ModelResponse, ToolCall, ToolMessage
from personal_agent.llm.base import ProviderError, ProviderTimeout
from personal_agent.tools.builtin import create_builtin_registry
from personal_agent.tools.registry import Tool, ToolArguments
from tests.fakes import FakeProvider, add_call


async def test_direct_text_and_conversation_history():
    provider = FakeProvider(ModelResponse(text="first answer"), ModelResponse(text="second answer"))
    context = ContextBuilder("custom system prompt")
    agent = Agent(provider, create_builtin_registry(), context)

    first = await agent.run("hello")
    second = await agent.run("follow up")

    assert (first.status, first.text, first.rounds) == ("completed", "first answer", 1)
    assert second.text == "second answer"
    assert [message.text for message in provider.requests[1]] == [
        "custom system prompt",
        "hello",
        "first answer",
        "follow up",
    ]
    assert len(context.history) == 4
    history = context.history
    history[0].text = "mutated"
    assert context.history[0].text == "hello"
    assert {tool.name for tool in provider.tools[0]} == {"add_numbers", "get_current_time"}


async def test_single_tool_result_is_sent_to_model_with_same_id():
    provider = FakeProvider(
        ModelResponse(text="Let me calculate.", tool_calls=[add_call("sum-1")]),
        ModelResponse(text="5"),
    )
    agent = Agent(provider, create_builtin_registry())

    result = await agent.run("2+3")

    assert (result.status, result.text, result.rounds) == ("completed", "5", 2)
    message = provider.requests[1][-1]
    assert isinstance(message, ToolMessage)
    assert message.result.call_id == "sum-1"
    assert message.result.ok
    assert message.result.data == {"sum": 5.0}
    assert provider.requests[1][-2].text == "Let me calculate."


async def test_sequential_model_tool_rounds():
    provider = FakeProvider(
        ModelResponse(tool_calls=[add_call("first")]),
        ModelResponse(tool_calls=[add_call("second", '{"a": 5, "b": 8}')]),
        ModelResponse(text="13"),
    )
    result = await Agent(provider, create_builtin_registry()).run("add twice")
    assert result.rounds == 3
    results = [
        message.result for message in provider.requests[-1] if isinstance(message, ToolMessage)
    ]
    assert [(item.call_id, item.data) for item in results] == [
        ("first", {"sum": 5.0}),
        ("second", {"sum": 13.0}),
    ]


async def test_batch_tools_preserve_order_and_call_ids():
    provider = FakeProvider(
        ModelResponse(
            tool_calls=[
                add_call("second-looking-id", '{"a": 10, "b": 20}'),
                add_call("first-looking-id", '{"a": 1, "b": 2}'),
            ]
        ),
        ModelResponse(text="30 and 3"),
    )
    result = await Agent(provider, create_builtin_registry()).run("two sums")
    outputs = [
        message.result for message in provider.requests[1] if isinstance(message, ToolMessage)
    ]
    assert result.status == "completed"
    assert [(item.call_id, item.data) for item in outputs] == [
        ("second-looking-id", {"sum": 30.0}),
        ("first-looking-id", {"sum": 3.0}),
    ]


@pytest.mark.parametrize(
    "call, code",
    [
        (ToolCall(id="missing", name="not_registered", arguments="{}"), "unknown_tool"),
        (add_call(arguments='{"a": "bad", "b": 3}'), "invalid_arguments"),
        (add_call(arguments="{broken json"), "invalid_arguments"),
    ],
)
async def test_tool_errors_return_to_model_and_loop_continues(call, code):
    provider = FakeProvider(ModelResponse(tool_calls=[call]), ModelResponse(text="Recovered"))
    result = await Agent(provider, create_builtin_registry()).run("request")
    output = provider.requests[1][-1].result
    assert result.status == "completed"
    assert not output.ok
    assert output.call_id == call.id
    assert output.error.code == code


async def test_failed_tool_does_not_skip_other_tools_in_batch():
    def fail(_: ToolArguments):
        raise RuntimeError("sensitive private exception")

    registry = create_builtin_registry()
    registry.register(Tool("fail", "Always fails", ToolArguments, fail, side_effect_free=True))
    provider = FakeProvider(
        ModelResponse(tool_calls=[ToolCall(id="bad", name="fail", arguments="{}"), add_call()]),
        ModelResponse(text="Partial success"),
    )
    result = await Agent(provider, registry).run("try both")
    assert result.status == "completed"
    outputs = [
        message.result for message in provider.requests[1] if isinstance(message, ToolMessage)
    ]
    assert outputs[0].error.code == "execution_error"
    assert outputs[1].ok


@pytest.mark.parametrize(
    "error,status",
    [
        (ProviderError("private response body"), "provider_error"),
        (ProviderTimeout("private timeout detail"), "timeout"),
        (TimeoutError(), "timeout"),
        (RuntimeError("private runtime detail"), "runtime_error"),
    ],
)
async def test_provider_errors_are_bounded_sanitized_and_session_reusable(error, status, caplog):
    caplog.set_level(logging.INFO, logger="personal_agent")
    provider = FakeProvider(error, ModelResponse(text="next turn works"))
    agent = Agent(provider, create_builtin_registry())
    first = await agent.run("first")
    assert first.status == status
    assert first.rounds == 1
    assert "private" not in first.model_dump_json() + caplog.text
    second = await agent.run("next")
    assert second.status == "completed"
    assert "Runtime stopped" in provider.requests[1][-2].text


async def test_runtime_enforces_model_timeout():
    class SlowProvider:
        cancelled = False

        async def generate(self, messages, tools):
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True

    provider = SlowProvider()
    agent = Agent(provider, create_builtin_registry(), request_timeout_seconds=0.01)
    result = await asyncio.wait_for(agent.run("hello"), timeout=1)
    assert result.status == "timeout"
    assert provider.cancelled


async def test_round_limit_rejects_final_tools_keeps_pairs_and_allows_next_turn():
    provider = FakeProvider(
        ModelResponse(tool_calls=[add_call("r1")]),
        ModelResponse(tool_calls=[add_call("r2")]),
        ModelResponse(text="next turn"),
    )
    agent = Agent(provider, create_builtin_registry(), max_rounds=2)
    result = await agent.run("loop")
    assert (result.status, result.rounds, len(provider.requests)) == ("max_rounds", 2, 2)
    history = agent.context.history
    calls = [
        call.id
        for message in history
        if isinstance(message, AssistantMessage)
        for call in message.tool_calls
    ]
    outputs = [message.result.call_id for message in history if isinstance(message, ToolMessage)]
    assert calls == outputs == ["r1", "r2"]
    results = [message.result for message in history if isinstance(message, ToolMessage)]
    assert results[0].ok
    assert not results[1].ok
    assert results[1].error.code == "round_budget_exhausted"
    assert provider.tools[0]
    assert provider.tools[1] == []
    assert (await agent.run("continue")).status == "completed"
    next_turn_outputs = [
        message.result for message in provider.requests[2] if isinstance(message, ToolMessage)
    ]
    assert next_turn_outputs == results


@pytest.mark.parametrize("call_count", [1, 2])
async def test_final_round_never_executes_new_side_effects_even_with_text(call_count):
    effects = []

    async def write(_):
        effects.append("written")
        return "done"

    registry = create_builtin_registry()
    registry.register(Tool("write", "Test side effect", ToolArguments, write))
    calls = [ToolCall(id=f"write-{i}", name="write", arguments="{}") for i in range(call_count)]
    provider = FakeProvider(ModelResponse(text="I wrote it", tool_calls=calls))
    agent = Agent(provider, registry, max_rounds=1)
    result = await agent.run("write")

    assert result.status == "max_rounds"
    assert result.rounds == len(provider.requests) == 1
    assert result.text == ""
    assert "not executed" in result.error
    assert provider.tools == [[]]
    assert effects == []
    outputs = [
        message.result for message in agent.context.history if isinstance(message, ToolMessage)
    ]
    assert [output.call_id for output in outputs] == [call.id for call in calls]
    assert all(
        not output.ok and output.error.code == "round_budget_exhausted" for output in outputs
    )


async def test_final_round_can_return_answer_after_processing_prior_tool_output():
    provider = FakeProvider(ModelResponse(tool_calls=[add_call()]), ModelResponse(text="5"))
    result = await Agent(provider, create_builtin_registry(), max_rounds=2).run("2+3")
    assert result.status == "completed"
    assert result.text == "5"
    assert provider.tools[1] == []
    assert provider.requests[1][-1].result.data == {"sum": 5.0}


@pytest.mark.parametrize(
    "response",
    [
        ModelResponse(),
        ModelResponse(text="   "),
        ModelResponse(tool_calls=[add_call("duplicate"), add_call("duplicate")]),
    ],
)
async def test_invalid_response_stops_before_tools_execute(response):
    agent = Agent(FakeProvider(response), create_builtin_registry())
    result = await agent.run("request")
    assert result.status == "invalid_response"
    assert not any(isinstance(message, ToolMessage) for message in agent.context.history)


async def test_reused_call_id_from_previous_round_rejected():
    provider = FakeProvider(
        ModelResponse(tool_calls=[add_call("same")]),
        ModelResponse(tool_calls=[add_call("same")]),
    )
    result = await Agent(provider, create_builtin_registry()).run("request")
    assert result.status == "invalid_response"
    assert result.rounds == 2


async def test_cancellation_propagates_and_preserves_previous_history():
    started = asyncio.Event()

    class BlockingProvider:
        async def generate(self, messages, tools):
            started.set()
            await asyncio.Event().wait()

    agent = Agent(BlockingProvider(), create_builtin_registry())
    task = asyncio.create_task(agent.run("cancel me"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent.context.history == []


async def test_blank_input_rejected_without_model_call():
    provider = FakeProvider()
    with pytest.raises(ValueError, match="empty"):
        await Agent(provider, create_builtin_registry()).run(" \n")
    assert provider.requests == []


def test_runtime_limits_validated():
    with pytest.raises(ValueError):
        Agent(FakeProvider(), create_builtin_registry(), max_rounds=0)
    with pytest.raises(ValueError):
        Agent(FakeProvider(), create_builtin_registry(), request_timeout_seconds=float("inf"))
    with pytest.raises(ValueError):
        Agent(FakeProvider(), create_builtin_registry(), max_tool_calls_per_response=0)


@pytest.mark.parametrize("earlier", [False, True])
async def test_host_tool_limit_rejects_whole_batch_and_preserves_prior_results(earlier):
    provider = FakeProvider(
        *([ModelResponse(tool_calls=[add_call("accepted")])] if earlier else []),
        ModelResponse(tool_calls=[add_call("rejected-1"), add_call("rejected-2")]),
        ModelResponse(text="next run"),
    )
    agent = Agent(
        provider,
        create_builtin_registry(),
        max_tool_calls_per_response=2 if earlier else 1,
        max_tool_calls_per_run=2,
    )
    result = await agent.run("synthetic batch")
    assert result.status == "invalid_response" and "limit exceeded" in result.error
    results = [m.result for m in agent.context.history if isinstance(m, ToolMessage)]
    assert [r.call_id for r in results] == (["accepted"] if earlier else [])
    assert all(
        c.id not in {"rejected-1", "rejected-2"}
        for m in agent.context.history
        for c in getattr(m, "tool_calls", [])
    )
    assert (await agent.run("new request")).text == "next run"
