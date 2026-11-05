"""Trusted execution scope and conservative shared request accounting."""

import asyncio
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from uuid import uuid4

from personal_agent.core.budget import ContextError, estimate_request
from personal_agent.core.models import TokenUsage
from personal_agent.llm.base import ProviderError


@dataclass
class ExecutionBudget:
    max_requests: int = 24
    max_tokens: int = 131072
    max_seconds: float = 180
    requests: int = 0
    reserved_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    max_tool_calls: int = 256
    tool_calls: int = 0
    usage_complete: bool = True
    started: float = field(default_factory=time.monotonic)

    def reserve(self, messages, tools, output: int | None) -> None:
        tokens = estimate_request(messages, tools) + (output or 4096)
        if (
            self.requests >= self.max_requests
            or self.reserved_tokens + tokens > self.max_tokens
            or time.monotonic() - self.started >= self.max_seconds
        ):
            raise ContextError("execution_budget", "Shared execution budget exhausted.")
        self.requests += 1
        self.reserved_tokens += tokens

    def reserve_tool(self) -> bool:
        if (
            self.tool_calls >= self.max_tool_calls
            or time.monotonic() - self.started >= self.max_seconds
        ):
            return False
        self.tool_calls += 1
        return True

    def observe(self, usage: TokenUsage | None) -> None:
        if usage is None:
            self.usage_complete = False
        else:
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens

    def report(self) -> dict:
        return {
            "requests": self.requests,
            "reserved_tokens": self.reserved_tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "usage_complete": self.usage_complete,
        }


@dataclass(frozen=True)
class RunScope:
    agent_id: str
    conversation_id: str
    source: str = "cli"
    permissions: frozenset[str] = frozenset()
    root_id: str = field(default_factory=lambda: uuid4().hex)
    budget: ExecutionBudget = field(default_factory=ExecutionBudget, compare=False)
    child: bool = False
    background: bool = False


CURRENT_SCOPE: ContextVar[RunScope | None] = ContextVar("execution_scope", default=None)
CURRENT_THREAD: ContextVar[str | None] = ContextVar("logical_thread", default=None)
ROUTING_MODEL_USED: ContextVar[bool] = ContextVar("routing_model_used", default=False)
ROUTING_CLARIFY: ContextVar[bool] = ContextVar("routing_clarify", default=False)
CURRENT_RUN: ContextVar[str | None] = ContextVar("execution_run", default=None)


class BudgetProvider:
    def __init__(self, provider, budget: ExecutionBudget) -> None:
        self.provider, self.budget = provider, budget

    def safe_compaction(self, messages) -> bool:
        check = getattr(self.provider, "safe_compaction", None)
        return bool(check and check(messages))

    async def generate(self, messages, tools, **options):
        self.budget.reserve(messages, tools, options.get("max_output_tokens"))
        usage = None
        try:
            remaining = self.budget.max_seconds - (time.monotonic() - self.budget.started)
            async with asyncio.timeout(max(0, remaining)) as deadline:
                response = await self.provider.generate(messages, tools, **options)
                if deadline.expired():
                    raise ContextError("execution_budget", "Shared execution deadline expired.")
                if asyncio.current_task().cancelling():
                    raise asyncio.CancelledError
            usage = response.usage
            return response
        except ProviderError as exc:
            usage = exc.diagnostics.usage if exc.diagnostics else None
            raise
        finally:
            self.budget.observe(usage)
