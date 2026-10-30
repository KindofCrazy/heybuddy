"""One bounded call ledger for automation only; never records private request bodies."""

from time import perf_counter

from personal_agent.automation.models import AutomationError
from personal_agent.core.budget import estimate_request
from personal_agent.core.models import Message, ModelResponse, ToolDefinition
from personal_agent.llm.base import Provider, ProviderError, ReasoningEffort


class MeteredProvider:
    def __init__(
        self, provider: Provider, max_requests: int, max_tokens: int, previous: dict | None = None
    ) -> None:
        self.provider = provider
        self.max_requests = max_requests
        self.max_tokens = max_tokens
        self.metrics = {
            "requests": 0,
            "reserved_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "usage_complete": True,
            "seconds": 0.0,
        }

        self.metrics.update(previous or {})
        self.initial_requests = self.metrics["requests"]
        self.initial_reserved = self.metrics["reserved_tokens"]
        self.blocked_code = ""

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        max_output_tokens: int | None = None,
        reasoning_effort: ReasoningEffort | None = None,
    ) -> ModelResponse:
        reserve = estimate_request(messages, tools) + (max_output_tokens or 0)
        if (
            self.metrics["requests"] - self.initial_requests >= self.max_requests
            or self.metrics["reserved_tokens"] - self.initial_reserved + reserve > self.max_tokens
        ):
            self.blocked_code = "automation_call_budget"
            raise AutomationError(self.blocked_code)
        self.metrics["requests"] += 1
        self.metrics["reserved_tokens"] += reserve
        start = perf_counter()
        usage = None
        try:
            response = await self.provider.generate(
                messages,
                tools,
                max_output_tokens=max_output_tokens,
                **({"reasoning_effort": reasoning_effort} if reasoning_effort is not None else {}),
            )
            usage = response.usage
            return response
        except ProviderError as error:
            usage = error.diagnostics.usage if error.diagnostics else None
            raise
        finally:
            self.metrics["seconds"] += perf_counter() - start
            if usage:
                self.metrics["input_tokens"] += usage.input_tokens
                self.metrics["output_tokens"] += usage.output_tokens
                self.metrics["reasoning_tokens"] += usage.reasoning_tokens or 0
            else:
                self.metrics["usage_complete"] = False
