from personal_agent.core.models import Message, ModelResponse, ToolCall, ToolDefinition


class FakeProvider:
    def __init__(self, *responses: ModelResponse | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[list[Message]] = []
        self.tools: list[list[ToolDefinition]] = []

    async def generate(self, messages: list[Message], tools: list[ToolDefinition]) -> ModelResponse:
        self.requests.append([message.model_copy(deep=True) for message in messages])
        self.tools.append(tools)
        if not self.responses:
            raise AssertionError("Unexpected extra model request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def add_call(call_id: str = "call_add", arguments: str = '{"a": 2, "b": 3}') -> ToolCall:
    return ToolCall(id=call_id, name="add_numbers", arguments=arguments)
