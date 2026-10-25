"""Plain internal data; no SDK response objects cross the provider boundary."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolCall(Model):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    # Keep the raw JSON so malformed model arguments become a recoverable tool error.
    arguments: str


class ToolDefinition(Model):
    name: str
    description: str
    parameters: dict[str, JsonValue]


class ToolError(Model):
    code: str
    message: str


class ToolResult(Model):
    call_id: str
    name: str
    ok: bool
    data: JsonValue = None
    error: ToolError | None = None


class TextMessage(Model):
    role: Literal["system", "user"]
    text: str


class AssistantMessage(Model):
    role: Literal["assistant"] = "assistant"
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    # Namespaced opaque JSON, owned only by the provider. For example, OpenAI
    # reasoning and message items must be replayed alongside tool outputs.
    provider_data: dict[str, list[dict[str, JsonValue]]] = Field(default_factory=dict, repr=False)


class ToolMessage(Model):
    role: Literal["tool"] = "tool"
    result: ToolResult


Message = Annotated[TextMessage | AssistantMessage | ToolMessage, Field(discriminator="role")]


class ModelResponse(Model):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    provider_data: dict[str, list[dict[str, JsonValue]]] = Field(default_factory=dict, repr=False)


class RunResult(Model):
    status: Literal[
        "completed",
        "max_rounds",
        "timeout",
        "provider_error",
        "invalid_response",
        "runtime_error",
        "storage_error",
        "memory_error",
    ]
    text: str = ""
    rounds: int
    error: str | None = None
