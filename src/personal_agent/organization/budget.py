"""A per-cycle reservation ledger shared by Episode, Thread and repair requests."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Protocol

from personal_agent.core.budget import estimate_request
from personal_agent.core.models import Message, ModelResponse, TokenUsage, ToolDefinition
from personal_agent.llm.base import Provider, ProviderError, ReasoningEffort
from personal_agent.organization.clock import Clock
from personal_agent.organization.models import OrganizationError, OrganizationPolicy


class MaintenanceBudget:
    def __init__(self, policy: OrganizationPolicy, clock: Clock) -> None:
        self.policy = policy
        self.clock = clock
        self.started = clock.monotonic()
        self.requests = self.reserved_tokens = 0
        self.input_tokens = self.output_tokens = self.reasoning_tokens = 0
        self.usage_complete = True
        self.exhausted = False

    def reserve(
        self, messages: list[Message], tools: list[ToolDefinition], output: int | None
    ) -> None:
        tokens = estimate_request(messages, tools) + (output or 0)
        if tokens > self.policy.maintenance_tokens:
            raise OrganizationError("maintenance_request_budget")
        if (
            self.requests >= self.policy.maintenance_requests
            or self.reserved_tokens + tokens > self.policy.maintenance_tokens
            or self.clock.monotonic() - self.started >= self.policy.maintenance_seconds
        ):
            self.exhausted = True
            raise OrganizationError("maintenance_budget")
        self.requests += 1
        self.reserved_tokens += tokens

    def observe(self, usage: TokenUsage | None) -> None:
        if usage is None:
            self.usage_complete = False
        else:
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens
            self.reasoning_tokens += usage.reasoning_tokens or 0

    def report(self) -> dict:
        return {
            "requests": self.requests,
            "reserved_tokens": self.reserved_tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "usage_complete": self.usage_complete,
            "exhausted": self.exhausted,
            "elapsed_seconds": max(0, self.clock.monotonic() - self.started),
        }


class CycleProvider:
    def __init__(self, provider: Provider, budget: MaintenanceBudget) -> None:
        self.provider = provider
        self.budget = budget

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        max_output_tokens: int | None = None,
        reasoning_effort: ReasoningEffort | None = None,
    ) -> ModelResponse:
        self.budget.reserve(messages, tools, max_output_tokens)
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
            self.budget.observe(usage)


class ProviderOwner(Protocol):
    provider: Provider


@contextmanager
def cycle_providers(models: Sequence[ProviderOwner], budget: MaintenanceBudget) -> Iterator[None]:
    """One serial Host scope; deduplicate shared models and always restore providers."""
    originals = {id(model): (model, model.provider) for model in models}
    try:
        for model, provider in originals.values():
            model.provider = CycleProvider(provider, budget)
        yield
    finally:
        for model, provider in originals.values():
            model.provider = provider
