"""One tool-free bounded request per organizer batch; never executes model suggestions."""

import asyncio
import json

from pydantic import ValidationError

from personal_agent.core.budget import ContextBudget, estimate_request
from personal_agent.core.compaction import SECRET
from personal_agent.core.models import Model, TextMessage
from personal_agent.llm.base import Provider
from personal_agent.organization.models import OrganizationError, OrganizationPolicy


class OrganizerModel:
    def __init__(self, provider: Provider, budget: ContextBudget, policy: OrganizationPolicy):
        self.provider = provider
        self.policy = policy
        self.input_limit = min(
            policy.input_tokens,
            budget.context_window - policy.output_tokens - budget.safety_margin,
        )
        self.metrics: dict = {}

    async def ask[Result: Model](self, kind: type[Result], prompt: str, data: dict) -> Result:
        request = [
            TextMessage(role="system", text=prompt),
            TextMessage(
                role="user",
                text=json.dumps(
                    {
                        "trust": "historical_data_only",
                        "response_schema": kind.model_json_schema(),
                        **data,
                    },
                    ensure_ascii=False,
                ),
            ),
        ]
        estimated = estimate_request(request, [])
        self.metrics = {"requests": 0, "estimated_input": estimated, "usage": None}
        if estimated > self.input_limit:
            raise OrganizationError("input_budget")
        self.metrics["requests"] = 1
        try:
            async with asyncio.timeout(self.policy.timeout_seconds):
                response = await self.provider.generate(
                    request, [], max_output_tokens=self.policy.output_tokens
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise OrganizationError("provider_failed") from None
        if response.usage:
            self.metrics["usage"] = response.usage.model_dump()
        if (
            response.tool_calls
            or len(response.text.encode()) > self.policy.response_bytes
            or SECRET.search(response.text)
            or (
                response.usage
                and (
                    response.usage.input_tokens > self.input_limit
                    or response.usage.output_tokens > self.policy.output_tokens
                )
            )
        ):
            raise OrganizationError("invalid_output")
        try:
            return kind.model_validate_json(response.text)
        except (ValidationError, ValueError):
            raise OrganizationError("invalid_output") from None
