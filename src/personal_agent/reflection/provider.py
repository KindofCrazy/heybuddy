"""Tool-free structured requests, independent of plans, approvals and memory writes."""

import asyncio
import json

from pydantic import ValidationError

from personal_agent.core.budget import ContextBudget, estimate_request
from personal_agent.core.evidence import SECRET
from personal_agent.core.models import Model, TextMessage, TokenUsage
from personal_agent.llm.base import Provider, ProviderError
from personal_agent.organization.models import OrganizationError
from personal_agent.reflection.models import (
    Extraction,
    ReflectionError,
    ReflectionPolicy,
    Selection,
)


def structured_request(kind: type[Model], prompt: str, data: dict) -> list[TextMessage]:
    return [
        TextMessage(role="system", text=prompt),
        TextMessage(
            role="user",
            text=json.dumps(
                {"trust": "data_only", "response_schema": kind.model_json_schema(), **data},
                ensure_ascii=False,
            ),
        ),
    ]


class ReflectionModel:
    def __init__(self, provider: Provider, budget: ContextBudget, policy: ReflectionPolicy) -> None:
        self.provider = provider
        self.policy = policy
        self.input_limit = min(
            policy.input_tokens, budget.context_window - policy.output_tokens - budget.safety_margin
        )
        self.reset_metrics()

    def reset_metrics(self) -> None:
        self.metrics = {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "usage_complete": True,
        }

    def _observe_usage(self, usage: TokenUsage | None) -> None:
        if usage is None:
            self.metrics["usage_complete"] = False
        else:
            for field in ("input_tokens", "output_tokens", "reasoning_tokens"):
                self.metrics[field] += getattr(usage, field) or 0

    async def ask[Result: Model](self, kind: type[Result], prompt: str, data: dict) -> Result:
        stage = {Extraction: "extraction", Selection: "selection"}.get(kind, "consolidation")
        if self.input_limit < 2048:
            raise ReflectionError("reflection_budget", stage=stage)
        request = structured_request(kind, prompt, data)
        if estimate_request(request, []) > self.input_limit:
            raise ReflectionError("reflection_input_budget", stage=stage)
        try:
            self.metrics["requests"] += 1
            async with asyncio.timeout(self.policy.timeout_seconds) as deadline:
                response = await self.provider.generate(
                    request, [], max_output_tokens=self.policy.output_tokens
                )
                if deadline.expired():
                    raise TimeoutError
                if asyncio.current_task().cancelling():
                    raise asyncio.CancelledError
        except asyncio.CancelledError:
            self._observe_usage(None)
            raise
        except ProviderError as error:
            self._observe_usage(error.diagnostics.usage if error.diagnostics else None)
            raise ReflectionError(
                "reflection_provider_error", stage=stage, diagnostics=error.diagnostics
            ) from None
        except OrganizationError as error:
            if error.code in {"maintenance_budget", "maintenance_request_budget"}:
                self.metrics["requests"] -= 1
            else:
                self._observe_usage(None)
            raise
        except Exception:
            self._observe_usage(None)
            raise ReflectionError("reflection_provider_error", stage=stage) from None
        self._observe_usage(response.usage)
        if (
            response.tool_calls
            or not response.text.strip()
            or len(response.text.encode()) > self.policy.response_max_bytes
            or SECRET.search(response.text)
            or (
                response.usage
                and (
                    response.usage.input_tokens > self.input_limit
                    or response.usage.output_tokens > self.policy.output_tokens
                )
            )
        ):
            raise ReflectionError("invalid_reflection_output", stage=stage)
        try:
            return kind.model_validate_json(response.text)
        except (ValidationError, ValueError):
            raise ReflectionError("invalid_reflection_output", stage=stage) from None
