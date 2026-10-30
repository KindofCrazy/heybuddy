"""Responses API mapping is entirely contained in this adapter."""

import base64
from typing import cast

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from openai.types.responses import ResponseInputParam, ToolParam
from pydantic import ValidationError

from personal_agent.core.images import ImageReader
from personal_agent.core.models import (
    AssistantMessage,
    Message,
    ModelResponse,
    ProviderDiagnostics,
    TextMessage,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    ToolMessage,
)
from personal_agent.llm.base import ProviderError, ProviderTimeout


class OpenAIProvider:
    def __init__(self, client: AsyncOpenAI, model: str, images: ImageReader | None = None) -> None:
        self.client = client
        self.model = model
        self.images = images

    @staticmethod
    def _input(messages: list[Message], images: ImageReader | None = None) -> list[dict]:
        references = [ref for m in messages if isinstance(m, TextMessage) for ref in m.images]
        if len(references) > 128 or sum(ref.size_bytes for ref in references) > 33554432:
            raise ProviderError("Image request exceeds Host limits.", code="image_limit")
        items: list[dict] = []
        for message in messages:
            if isinstance(message, TextMessage):
                if message.images:
                    if images is None:
                        raise ProviderError(
                            "Image input is not configured.", code="image_unavailable"
                        )
                    content = [{"type": "input_text", "text": message.text}] if message.text else []
                    for ref in message.images:
                        encoded = base64.b64encode(images.read_image(ref)).decode("ascii")
                        content.append(
                            {
                                "type": "input_image",
                                "image_url": "data:image/jpeg;base64," + encoded,
                                "detail": "high",
                            }
                        )
                    items.append({"role": message.role, "content": content})
                else:
                    items.append({"role": message.role, "content": message.text})
            elif isinstance(message, ToolMessage):
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.result.call_id,
                        "output": message.result.model_dump_json(exclude_none=True),
                    }
                )
            elif isinstance(message, AssistantMessage):
                if "openai" in message.provider_data:
                    # Includes encrypted reasoning, refusals, text, and function calls
                    # in their original order. Never add the normalized calls twice.
                    items.extend(message.provider_data["openai"])
                else:
                    if message.text:
                        items.append({"role": "assistant", "content": message.text})
                    items.extend(
                        {
                            "type": "function_call",
                            "call_id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                        }
                        for call in message.tool_calls
                    )
        return items

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        max_output_tokens: int | None = None,
    ) -> ModelResponse:
        definitions = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "strict": False,
            }
            for tool in tools
        ]
        # strict=False preserves Pydantic defaults/optional fields without rewriting
        # arbitrary schemas. ToolRegistry always validates locally before execution.
        options = {"max_output_tokens": max_output_tokens} if max_output_tokens is not None else {}
        try:
            response = await self.client.responses.create(
                model=self.model,
                input=cast(ResponseInputParam, self._input(messages, self.images)),
                tools=cast(list[ToolParam], definitions),
                store=False,
                include=["reasoning.encrypted_content"],
                truncation="disabled",
                **options,
            )
        except APITimeoutError:
            raise ProviderTimeout("OpenAI request timed out.") from None
        except APIStatusError as exc:
            raise ProviderError(
                "OpenAI API error.", code="api_error", status_code=exc.status_code
            ) from None
        except APIConnectionError:
            raise ProviderError("Could not connect to OpenAI.", code="connection_error") from None
        except Exception:
            # SDK messages can include request bodies; never pass them to users/logs.
            raise ProviderError("OpenAI request failed.") from None

        try:
            usage = (
                TokenUsage(
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    reasoning_tokens=getattr(
                        getattr(response.usage, "output_tokens_details", None),
                        "reasoning_tokens",
                        None,
                    ),
                )
                if response.usage
                else None
            )
        except (AttributeError, TypeError, ValidationError):
            raise ProviderError(
                "OpenAI returned malformed usage.", code="malformed_response"
            ) from None
        if response.status != "completed":
            status = (
                response.status
                if isinstance(response.status, str)
                and response.status in {"incomplete", "failed", "in_progress", "cancelled"}
                else "unknown"
            )
            reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
            safe_reason = (
                reason
                if isinstance(reason, str) and reason in {"max_output_tokens", "content_filter"}
                else ("unknown" if reason is not None else None)
            )
            raise ProviderError(
                "OpenAI response did not complete.",
                code="incomplete_response",
                diagnostics=ProviderDiagnostics(
                    status=status, incomplete_reason=safe_reason, usage=usage
                ),
            )
        try:
            calls = []
            refusals = []
            for item in response.output:
                if item.type == "function_call":
                    calls.append(
                        ToolCall(id=item.call_id, name=item.name, arguments=item.arguments)
                    )
                elif item.type == "message":
                    refusals.extend(part.refusal for part in item.content if part.type == "refusal")
                elif item.type != "reasoning":
                    raise ProviderError(
                        "OpenAI returned an unsupported output item.", code="unsupported_output"
                    )
            return ModelResponse(
                usage=usage,
                text=response.output_text or "\n".join(refusals),
                tool_calls=calls,
                provider_data={
                    "openai": [
                        item.model_dump(mode="json", exclude_none=True) for item in response.output
                    ]
                },
            )
        except (AttributeError, TypeError, ValidationError):
            raise ProviderError(
                "OpenAI returned a malformed response.", code="malformed_response"
            ) from None
