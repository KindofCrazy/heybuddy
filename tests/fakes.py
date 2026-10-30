from personal_agent.core.models import Message, ModelResponse, ToolCall, ToolDefinition
from personal_agent.llm.base import ReasoningEffort


class FakeProvider:
    def __init__(self, *responses: ModelResponse | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[list[Message]] = []
        self.tools: list[list[ToolDefinition]] = []
        self.output_limits: list[int | None] = []
        self.reasoning_efforts: list[ReasoningEffort | None] = []

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        max_output_tokens: int | None = None,
        reasoning_effort: ReasoningEffort | None = None,
    ) -> ModelResponse:
        self.requests.append([message.model_copy(deep=True) for message in messages])
        self.tools.append(tools)
        self.output_limits.append(max_output_tokens)
        self.reasoning_efforts.append(reasoning_effort)
        if not self.responses:
            raise AssertionError("Unexpected extra model request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def add_call(call_id: str = "call_add", arguments: str = '{"a": 2, "b": 3}') -> ToolCall:
    return ToolCall(id=call_id, name="add_numbers", arguments=arguments)
