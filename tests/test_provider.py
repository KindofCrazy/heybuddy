import json

import httpx
import pytest
from openai import AsyncOpenAI

from personal_agent.core.agent import Agent
from personal_agent.core.models import AssistantMessage, TextMessage
from personal_agent.llm.base import ProviderError, ProviderTimeout
from personal_agent.llm.openai_provider import OpenAIProvider
from personal_agent.tools.builtin import create_builtin_registry


def response_body(output, *, status="completed"):
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 1,
        "model": "test-model",
        "status": status,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }


def text_item(text="done"):
    return {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def call_item(call_id="call_1"):
    return {
        "type": "function_call",
        "id": f"fc_{call_id}",
        "call_id": call_id,
        "name": "add_numbers",
        "arguments": '{"a":2,"b":3}',
        "status": "completed",
    }


def make_client(handler):
    return AsyncOpenAI(
        api_key="test-only-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )


async def test_maps_text_request_tools_and_response_without_sdk_leak():
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        assert request.url.path == "/v1/responses"
        return httpx.Response(200, json=response_body([text_item("hello")]))

    async with make_client(handle) as client:
        response = await OpenAIProvider(client, "test-model").generate(
            [TextMessage(role="user", text="hi")], create_builtin_registry().definitions()
        )

    assert response.text == "hello"
    assert response.tool_calls == []
    assert requests[0]["model"] == "test-model"
    assert requests[0]["input"] == [{"role": "user", "content": "hi"}]
    assert requests[0]["store"] is False
    assert "reasoning.encrypted_content" in requests[0]["include"]
    assert requests[0]["tools"][1]["name"] == "add_numbers"
    assert requests[0]["tools"][1]["strict"] is False
    # model_dump_json succeeds: no SDK objects in the internal response.
    assert json.loads(response.model_dump_json())["provider_data"]["openai"][0]["id"] == "msg_1"


async def test_actual_sdk_multi_call_loop_replays_reasoning_and_matches_outputs():
    requests = []
    reasoning = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [],
        "encrypted_content": "opaque-test-content",
    }

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json=response_body(
                    [reasoning, text_item("calculating"), call_item("alpha"), call_item("beta")]
                ),
            )
        return httpx.Response(200, json=response_body([text_item("both are 5")]))

    async with make_client(handle) as client:
        result = await Agent(OpenAIProvider(client, "test-model"), create_builtin_registry()).run(
            "two sums"
        )
    assert result.text == "both are 5"
    items = requests[1]["input"]
    assert items[2] == reasoning
    calls = [item for item in items if item.get("type") == "function_call"]
    outputs = [item for item in items if item.get("type") == "function_call_output"]
    assert [item["call_id"] for item in calls] == ["alpha", "beta"]
    assert [item["call_id"] for item in outputs] == ["alpha", "beta"]
    assert all(json.loads(item["output"])["data"] == {"sum": 5.0} for item in outputs)
    assert len(calls) == 2  # Normalized calls must not be duplicated during replay.


@pytest.mark.parametrize("status", [400, 401, 429, 500])
async def test_api_error_does_not_leak_body_and_does_not_retry(status):
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(status, json={"error": {"message": "private-body-token"}})

    async with make_client(handle) as client:
        with pytest.raises(ProviderError) as caught:
            await OpenAIProvider(client, "test-model").generate([], [])
    assert caught.value.status_code == status
    assert caught.value.code == "api_error"
    assert "private-body-token" not in str(caught.value)
    assert count == 1


@pytest.mark.parametrize(
    "error, expected",
    [
        (httpx.ReadTimeout("private timeout"), ProviderTimeout),
        (httpx.ConnectError("private network detail"), ProviderError),
    ],
)
async def test_sdk_timeout_and_connection_mapping(error, expected):
    def handle(request):
        raise error

    async with make_client(handle) as client:
        with pytest.raises(expected) as caught:
            await OpenAIProvider(client, "test-model").generate([], [])
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("status", ["incomplete", "failed", "cancelled", "in_progress"])
async def test_incomplete_response_is_not_executed(status):
    def handle(request):
        return httpx.Response(200, json=response_body([call_item()], status=status))

    async with make_client(handle) as client:
        with pytest.raises(ProviderError, match="did not complete"):
            await OpenAIProvider(client, "test-model").generate([], [])


async def test_refusal_is_user_visible_text():
    item = {
        "type": "message",
        "id": "msg_refusal",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "refusal", "refusal": "Cannot help."}],
    }
    async with make_client(lambda _: httpx.Response(200, json=response_body([item]))) as client:
        response = await OpenAIProvider(client, "test-model").generate([], [])
    assert response.text == "Cannot help."


def test_normalized_assistant_fallback_can_be_encoded():
    from tests.fakes import add_call

    items = OpenAIProvider._input([AssistantMessage(text="working", tool_calls=[add_call()])])
    assert items[0] == {"role": "assistant", "content": "working"}
    assert items[1]["call_id"] == "call_add"


async def test_compatible_endpoint_replays_plain_reasoning_without_sdk_objects():
    requests = []
    reasoning = {
        "type": "reasoning",
        "id": "rs_plain",
        "summary": [],
        "content": [{"type": "reasoning_text", "text": "test-only reasoning"}],
    }

    def handle(request):
        assert request.url.host == "api.deepseek.com"
        assert request.url.path == "/responses"
        requests.append(json.loads(request.content))
        output = [reasoning, call_item()] if len(requests) == 1 else [text_item()]
        return httpx.Response(200, json=response_body(output))

    async with AsyncOpenAI(
        api_key="test-only-key",
        base_url="https://api.deepseek.com",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        max_retries=0,
    ) as client:
        result = await Agent(
            OpenAIProvider(client, "deepseek-flash"), create_builtin_registry()
        ).run("sum")
    assert result.status == "completed"
    assert requests[1]["model"] == "deepseek-flash"
    assert requests[1]["input"][2]["content"] == reasoning["content"]


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("max_output_tokens", "max_output_tokens"),
        ("content_filter", "content_filter"),
        ("private provider body must not leak", "unknown"),
    ],
)
async def test_incomplete_response_diagnostics_are_safe_and_not_retried(reason, expected):
    calls = []

    def handle(request):
        calls.append(request)
        body = response_body([text_item("private incomplete body")], status="incomplete")
        body["incomplete_details"] = {"reason": reason}
        body["usage"] = {
            "input_tokens": 2176,
            "output_tokens": 4096,
            "total_tokens": 6272,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 3800},
        }
        return httpx.Response(200, json=body)

    async with make_client(handle) as client:
        with pytest.raises(ProviderError) as error:
            await OpenAIProvider(client, "test-model").generate([], [], max_output_tokens=4096)
    diagnostic = error.value.diagnostics
    assert diagnostic.model_dump() == {
        "status": "incomplete",
        "incomplete_reason": expected,
        "usage": {"input_tokens": 2176, "output_tokens": 4096, "reasoning_tokens": 3800},
    }
    assert "private" not in str(error.value) and "private" not in diagnostic.model_dump_json()
    assert len(calls) == 1


async def test_success_reports_reasoning_count_and_tolerates_missing_details():
    for details in (None, {"reasoning_tokens": 3086}):
        body = response_body([text_item("proposal")])
        body["usage"] = {"input_tokens": 2176, "output_tokens": 3596, "total_tokens": 5772}
        if details is not None:
            body["usage"]["output_tokens_details"] = details
        async with make_client(lambda _, payload=body: httpx.Response(200, json=payload)) as client:
            response = await OpenAIProvider(client, "test-model").generate([], [])
        assert response.usage.input_tokens == 2176 and response.usage.output_tokens == 3596
        assert response.usage.reasoning_tokens == (3086 if details else None)


async def test_malformed_reasoning_usage_is_sanitized():
    body = response_body([text_item("private response")])
    body["usage"] = {
        "input_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 30,
        "output_tokens_details": {"reasoning_tokens": -1},
    }
    async with make_client(lambda _, payload=body: httpx.Response(200, json=payload)) as client:
        with pytest.raises(ProviderError) as error:
            await OpenAIProvider(client, "test-model").generate([], [])
    assert error.value.code == "malformed_response" and "private" not in str(error.value)
