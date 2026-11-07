"""Input-only, serial inference, bounded accounting and isolated temporary state."""

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path
from time import perf_counter

from personal_agent.core.budget import estimate_request
from personal_agent.core.models import ModelResponse
from personal_agent.evaluation.heybuddy_bench import adapters, baselines
from personal_agent.evaluation.runner import Budget, EvaluationLimit
from personal_agent.llm.base import ProviderError


class ContractProvider:
    """Gold-independent conservative responses, solely for exercising production wiring."""

    async def generate(self, messages, tools, **options):
        data = json.loads(messages[-1].text)
        schema = data.get("response_schema", {}).get("title")
        if schema == "Extraction":
            result = {"candidates": []}
        elif schema == "AttentionDecision":
            result = dict(
                action="silent",
                proposal_ids=[data["candidates"][0]["id"]],
                reason="Fixed offline contract response",
            )
        else:
            result = dict(action="clarify", thread_id=None)
        return ModelResponse(text=json.dumps(result))


class Meter:
    def __init__(self, provider, budget: Budget, *, offline=False):
        self.provider, self.budget, self.offline = provider, budget, offline
        self.calls = []

    async def generate(self, messages, tools, *, max_output_tokens=None, **options):
        if not self.offline and not self.budget.usage_complete:
            raise EvaluationLimit("unknown_usage")
        if tools:
            raise EvaluationLimit("benchmark_tools_forbidden")
        self.budget.reserve(messages, [], max_output_tokens)
        data = json.loads(messages[-1].text)
        record = dict(
            schema=data.get("response_schema", {}).get("title", "RouteChoice"),
            estimated_input_tokens=estimate_request(messages, []),
            prompt_sha256=hashlib.sha256(messages[0].text.encode()).hexdigest(),
            output_limit=max_output_tokens,
            status="attempted",
            usage=None,
        )
        self.calls.append(record)
        try:
            response = await self.provider.generate(
                messages, [], max_output_tokens=max_output_tokens, **options
            )
        except ProviderError as error:
            usage = error.diagnostics.usage if error.diagnostics else None
            self.budget.observe(usage)
            record.update(
                status="failed", error_code=error.code, usage=usage.model_dump() if usage else None
            )
            raise
        except BaseException:
            self.budget.observe(None)
            record["status"] = "failed"
            raise
        self.budget.observe(response.usage)
        record.update(
            status="completed", usage=response.usage.model_dump() if response.usage else None
        )
        # Only structured visible decisions, never SDK payload/reasoning or the raw exception.
        if not response.tool_calls and len(response.text.encode()) <= 32768:
            try:
                record["parsed"] = json.loads(response.text)
            except ValueError:
                record["status"] = "invalid_json"
        return response

    def usage(self, calls):
        known = all(c["usage"] is not None for c in calls)
        inp = sum(c["usage"]["input_tokens"] for c in calls if c["usage"])
        out = sum(c["usage"]["output_tokens"] for c in calls if c["usage"])
        reasoning = sum(c["usage"].get("reasoning_tokens", 0) or 0 for c in calls if c["usage"])
        priced = self.budget.input_price is not None and self.budget.output_price is not None
        return dict(
            provider_calls=len(calls),
            api_requests=0 if self.offline else len(calls),
            usage_complete=known,
            input_tokens=inp if known else None,
            output_tokens=out if known else None,
            reasoning_tokens=reasoning if known else None,
            estimated_input_tokens=sum(c["estimated_input_tokens"] for c in calls),
            api_cost_usd=0
            if self.offline
            else (
                (inp * self.budget.input_price + out * self.budget.output_price) / 1_000_000
                if known and priced
                else None
            ),
        )


async def run_cases(
    cases, *, mode: str, temp_parent: Path, provider=None, budget=None, on_result=None
):
    """No gold argument/path. Each case owns a new database and MemoryFS directory."""
    if mode not in {"baseline", "smoke", "live"}:
        raise ValueError("Unknown evaluation mode")
    if mode == "live" and provider is None:
        raise ValueError("Live requires explicit Provider")
    temp_parent.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - short local setup
    meter = Meter(provider or ContractProvider(), budget or Budget(), offline=mode != "live")
    results = []
    for case in cases:
        start, call_start = perf_counter(), len(meter.calls)
        error = None
        try:
            async with asyncio.timeout(300):
                if mode == "baseline":
                    prediction = baselines.predict(case["track"], case["input"])
                else:
                    with tempfile.TemporaryDirectory(
                        prefix="heybuddy-bench-", dir=temp_parent
                    ) as name:
                        prediction = await adapters.evaluate(
                            case["track"], case["input"], meter, Path(name)
                        )
        except (Exception, TimeoutError) as exc:
            # Type/code only: a Provider or Pydantic exception may contain credentials/input.
            error = exc.code if isinstance(exc, ProviderError) else type(exc).__name__
            prediction = {"action": "__error__"}
        results.append(
            dict(
                case_id=case["case_id"],
                track=case["track"],
                method=mode,
                prediction=prediction,
                error_code=error,
                latency_ms=round((perf_counter() - start) * 1000, 2),
                usage=meter.usage(meter.calls[call_start:]),
                calls=meter.calls[call_start:],
            )
        )
        if on_result is not None:
            on_result(results[-1])
        # Production wrappers may convert a budget exception into a normal failure result.
        if meter.budget.exhausted:
            break
        if mode == "live" and not meter.budget.usage_complete:
            break  # Unknown billed usage never grants more model requests.
    return results
