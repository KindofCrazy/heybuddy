"""Explicit, bounded live smoke. Production routing and prompts remain untouched."""

import asyncio
import math
from collections import Counter
from pathlib import Path
from time import perf_counter
from typing import Literal

from pydantic import Field

from personal_agent.core.budget import estimate_request
from personal_agent.evaluation.long_horizon.dataset import Corpus, Dataset, Gold, Splits, digest
from personal_agent.evaluation.long_horizon.metrics import SCORER_VERSION, aggregate, score
from personal_agent.evaluation.long_horizon.models import (
    BenchmarkProtocol,
    Config,
    ReplayResult,
    Schema,
)
from personal_agent.evaluation.long_horizon.replay import replay
from personal_agent.evaluation.long_horizon.report import (
    evaluation_code_hash,
    prepare_output,
    working_tree_dirty,
    write,
)
from personal_agent.evaluation.long_horizon.routing_eval import RoutingAdapter
from personal_agent.evaluation.runner import Budget
from personal_agent.llm.base import ProviderError
from personal_agent.memory.privacy import require_private_directory


class LiveStopped(Exception):
    """A terminal evaluation condition, including errors swallowed by the Router."""


class LiveConfig(Schema):
    variant: Literal["model-assisted"] = "model-assisted"
    replay_mode: Literal["streaming"] = "streaming"
    network: Literal[True] = True
    model: str = Field(min_length=1)
    endpoint_host: str = Field(min_length=1)
    context_window: int = Field(ge=4096)
    repetitions: int = Field(default=5, ge=1, le=5)
    max_requests: int = Field(default=5, ge=1, le=5)
    max_tokens: int = Field(default=20000, ge=1, le=20000)
    max_input_tokens: int = Field(default=2048, ge=1, le=2048)
    max_cost_cny: float = Field(default=10.0, gt=0, le=10, allow_inf_nan=False)
    input_price_cny: float = Field(gt=0, allow_inf_nan=False)
    output_price_cny: float = Field(gt=0, allow_inf_nan=False)
    price_source: str = Field(min_length=1)
    output_limit: Literal[1024] = 1024
    timeout_seconds: Literal[120] = 120

    def request_limits(self, stage: str) -> tuple[int, int]:
        return self.max_input_tokens, self.output_limit


class DevLiveConfig(LiveConfig):
    """Explicitly authorized one-pass routing-v1 dev profile; smoke limits stay intact."""

    profile: Literal["routing-dev-v1"] = "routing-dev-v1"
    repetitions: Literal[1] = 1
    max_requests: int = Field(default=136, ge=1, le=136)
    max_tokens: int = Field(default=696320, ge=1, le=696320)
    max_input_tokens: int = Field(default=4096, ge=1, le=4096)
    max_cost_cny: float = Field(default=3.0, gt=0, le=3, allow_inf_nan=False)
    timeout_seconds: Literal[1800] = 1800


class NoThinkingDevConfig(DevLiveConfig):
    profile: Literal["routing-dev-v1-no-thinking"] = "routing-dev-v1-no-thinking"
    reasoning_effort: Literal["none"] = "none"


def live_version(config: LiveConfig) -> str:
    if isinstance(config, NoThinkingDevConfig):
        return "live-dev-no-thinking-1"
    return "live-dev-1" if isinstance(config, DevLiveConfig) else "live-smoke-1"


class Trial(Schema):
    variant: Literal["rule", "model-assisted"]
    repetition: int = Field(ge=1, le=5)
    result: ReplayResult


class LiveBundle(Schema):
    version: Literal["live-smoke-1", "live-dev-1", "live-dev-no-thinking-1"] = "live-smoke-1"
    evaluation_type: Literal["Real Model Evaluation"] = "Real Model Evaluation"
    corpus: Corpus
    gold: Gold
    splits: Splits
    config: Config
    protocol: BenchmarkProtocol | None = None
    live_config: NoThinkingDevConfig | DevLiveConfig | LiveConfig
    split: str
    scenario_id: str | None
    hashes: dict[str, str]
    git_commit: str | None
    git_dirty: bool
    code_hash: str
    trials: list[Trial]
    calls: list[dict]
    usage: dict


class MeteredProvider:
    """One shared budget; persist attempts before dispatch, never persist raw reasoning."""

    def __init__(self, provider, config: LiveConfig, *, on_change=lambda: None):
        self.provider, self.config = provider, config
        self.budget = Budget(
            config.max_requests,
            config.max_tokens,
            input_price=config.input_price_cny,
            output_price=config.output_price_cny,
            max_cost=config.max_cost_cny,
        )
        self.calls: list[dict] = []
        self.stopped: str | None = None
        self.scope = {}
        self.on_change = on_change

    def usage(self) -> dict:
        return {
            "currency": "CNY",
            "actual_requests": self.budget.requests,
            "reserved_tokens": self.budget.reserved_tokens,
            "reserved_cost_cny": self.budget.reserved_cost,
            "usage_complete": self.budget.usage_complete,
            "input_tokens": self.budget.input_tokens if self.budget.usage_complete else "unknown",
            "output_tokens": self.budget.output_tokens if self.budget.usage_complete else "unknown",
            "cost_cny_upper_estimate": (
                (
                    self.budget.input_tokens * self.config.input_price_cny
                    + self.budget.output_tokens * self.config.output_price_cny
                )
                / 1_000_000
                if self.budget.usage_complete
                else "unknown"
            ),
        }

    async def generate(self, messages, tools, *, max_output_tokens=None, **options):
        if self.stopped:
            raise LiveStopped(self.stopped)
        estimated = estimate_request(messages, tools)
        input_limit, output_limit = self.config.request_limits(self.scope.get("stage", "routing"))
        if (
            tools
            or max_output_tokens != output_limit
            or estimated > input_limit
            or estimated + output_limit + 1024 > self.config.context_window
        ):
            self.stopped = "request_limit"
            raise LiveStopped(self.stopped)
        try:
            self.budget.reserve(messages, tools, max_output_tokens)
        except Exception:
            self.stopped = "budget_exhausted"
            raise LiveStopped(self.stopped) from None
        if getattr(self.config, "reasoning_effort", None) is not None:
            options = {**options, "reasoning_effort": self.config.reasoning_effort}
        record = dict(
            **self.scope,
            status="running",
            estimated_input_tokens=estimated,
            output_limit=max_output_tokens,
            prompt_hash=digest(messages[0].text),
            messages=[m.model_dump(mode="json") for m in messages],
            text=None,
            usage=None,
            error_code=None,
            provider_diagnostics=None,
            request_options={"max_output_tokens": max_output_tokens, **options},
        )
        if getattr(self.config, "json_mode", False):
            record["output_format"] = "json_object"
        self.calls.append(record)
        start = perf_counter()
        usage = None
        try:
            self.on_change()  # Failure here stops before external dispatch.
            response = await self.provider.generate(
                messages, [], max_output_tokens=max_output_tokens, **options
            )
            usage = response.usage
            record.update(text=response.text, status="completed")
            if response.tool_calls:
                self.stopped = "unexpected_tool_calls"
            if usage is None:
                self.stopped = "usage_unknown"
            elif usage.input_tokens > input_limit or usage.output_tokens > output_limit:
                self.stopped = "actual_token_limit"
            return response
        except BaseException as error:
            if isinstance(error, ProviderError) and error.diagnostics:
                usage = error.diagnostics.usage
                record["provider_diagnostics"] = error.diagnostics.model_dump(mode="json")
            record.update(
                status="failed",
                error_code=(
                    error.code if isinstance(error, ProviderError) else type(error).__name__
                ),
            )
            self.stopped = record["error_code"]
            raise
        finally:
            self.budget.observe(usage)
            record["usage"] = usage.model_dump(mode="json") if usage else None
            record["latency_ms"] = (perf_counter() - start) * 1000
            actual = self.usage()["cost_cny_upper_estimate"]
            if self.budget.input_tokens + self.budget.output_tokens > self.config.max_tokens:
                self.stopped = "actual_token_limit"
            if actual != "unknown" and actual > self.config.max_cost_cny:
                self.stopped = "actual_cost_limit"
            self.on_change()


class LiveRoutingAdapter(RoutingAdapter):
    def __init__(self, provider: MeteredProvider, repetition: int):
        super().__init__(provider=provider, model_selection=True)
        self.repetition = repetition

    async def route(self, repository, run_id, turn, scenario_id):
        self.provider.scope = dict(
            scenario_id=scenario_id, turn_id=turn.turn_id, repetition=self.repetition
        )
        before = len(self.provider.calls)
        prediction = await super().route(repository, run_id, turn, scenario_id)
        if (
            self.provider.stopped == "CancelledError"
            and prediction.routing_reason == "selection_unavailable"
        ):
            self.provider.stopped = "router_timeout"
            self.provider.calls[-1]["error_code"] = "router_timeout"
        if self.provider.stopped:
            raise LiveStopped(self.provider.stopped)
        calls = self.provider.calls[before:]
        usage = calls[0]["usage"] if calls else None
        return prediction.model_copy(
            update={
                "input_tokens": usage["input_tokens"] if usage else None,
                "output_tokens": usage["output_tokens"] if usage else None,
            }
        )


def live_summary(bundle: LiveBundle) -> dict:
    dataset = Dataset(bundle.corpus, bundle.gold, bundle.splits, bundle.config, bundle.protocol)
    validate_live_scope(dataset, bundle.live_config, bundle.split, bundle.scenario_id)
    if bundle.version != live_version(bundle.live_config):
        raise ValueError("Live budget profile/version mismatch")
    if bundle.hashes != {
        **dataset.hashes,
        "live_config_hash": digest(bundle.live_config.model_dump(mode="json")),
    }:
        raise ValueError("Live provenance mismatch")
    scenarios = [s for s in dataset.select(bundle.split, bundle.scenario_id) if "routing" in s.tags]
    expected = [
        (v, n, s.scenario_id)
        for n in range(1, bundle.live_config.repetitions + 1)
        for v in ("rule", "model-assisted")
        for s in scenarios
    ]
    if [(t.variant, t.repetition, t.result.scenario_id) for t in bundle.trials] != expected:
        raise ValueError("Live trial coverage incomplete")
    if not bundle.usage["usage_complete"] or any(c["status"] != "completed" for c in bundle.calls):
        raise ValueError("Live calls incomplete")
    usage_rows = [c.get("usage") for c in bundle.calls]
    if any(u is None for u in usage_rows):
        raise ValueError("Live usage missing")
    inputs = sum(u["input_tokens"] for u in usage_rows)
    outputs = sum(u["output_tokens"] for u in usage_rows)
    cost = (
        inputs * bundle.live_config.input_price_cny + outputs * bundle.live_config.output_price_cny
    ) / 1_000_000
    if (
        bundle.usage["input_tokens"] != inputs
        or bundle.usage["output_tokens"] != outputs
        or abs(bundle.usage["cost_cny_upper_estimate"] - cost) > 1e-10
        or cost > bundle.live_config.max_cost_cny
        or len(bundle.calls) > bundle.live_config.max_requests
        or inputs + outputs > bundle.live_config.max_tokens
        or any(
            u["input_tokens"] > bundle.live_config.max_input_tokens or u["output_tokens"] > 1024
            for u in usage_rows
        )
    ):
        raise ValueError("Live usage/budget mismatch")
    predictions = []
    metrics = {"rule": [], "model-assisted": []}
    trials = []
    for trial in bundle.trials:
        scenario = next(s for s in scenarios if s.scenario_id == trial.result.scenario_id)
        trial.result.validate_for(scenario)
        scored = score(trial.result.predictions, dataset.gold_for(scenario.scenario_id))
        metrics[trial.variant].append(scored)
        predictions.extend(trial.result.predictions if trial.variant == "model-assisted" else [])
        trials.append(
            dict(
                variant=trial.variant,
                repetition=trial.repetition,
                scenario_id=scenario.scenario_id,
                metrics=scored,
            )
        )
    if sum(p.request_count for p in predictions) != len(bundle.calls) or bundle.usage[
        "actual_requests"
    ] != len(bundle.calls):
        raise ValueError("Live request count mismatch")
    known = {
        (t.result.scenario_id, p.turn_id, t.repetition): p
        for t in bundle.trials
        if t.variant == "model-assisted"
        for p in t.result.predictions
    }
    call_keys = [(c["scenario_id"], c["turn_id"], c["repetition"]) for c in bundle.calls]
    if len(set(call_keys)) != len(call_keys) or any(
        key not in known or known[key].request_count != 1 for key in call_keys
    ):
        raise ValueError("Live call scope mismatch")
    latency = sorted(c["latency_ms"] for c in bundle.calls)
    return {
        "evaluation_type": bundle.evaluation_type,
        "scorer_version": SCORER_VERSION,
        "status": "completed",
        "purpose": (
            "one-pass dev evaluation with explicit reasoning.effort=none"
            if isinstance(bundle.live_config, NoThinkingDevConfig)
            else "one-pass dev evaluation on provisional synthetic gold"
            if isinstance(bundle.live_config, DevLiveConfig)
            else "connection smoke, not a formal capability benchmark"
        ),
        "annotation_status": (
            "human-reviewed"
            if bundle.protocol.human_reviewed
            else "agent-authored provisional gold"
        )
        if bundle.protocol
        else "legacy",
        "live_config": bundle.live_config.model_dump(mode="json"),
        **bundle.hashes,
        "git_commit": bundle.git_commit,
        "git_dirty": bundle.git_dirty,
        "code_hash": bundle.code_hash,
        "trials": trials,
        "variants": {v: aggregate(rows) for v, rows in metrics.items()},
        "usage": bundle.usage,
        "routing_reasons": dict(Counter(p.routing_reason for p in predictions)),
        "model_decision_support": len(bundle.calls),
        "rule_decision_support": sum(p.request_count == 0 for p in predictions),
        "selection_unavailable": sum(
            p.routing_reason == "selection_unavailable" for p in predictions
        ),
        "model_clarifications": sum(p.routing_reason == "model_clarify" for p in predictions),
        "prompt_hashes": sorted({c["prompt_hash"] for c in bundle.calls}),
        "model_latency_ms": {
            "support": len(latency),
            "p50": latency[math.ceil(0.5 * len(latency)) - 1] if len(latency) >= 2 else None,
            "p95": latency[math.ceil(0.95 * len(latency)) - 1] if len(latency) >= 20 else None,
        },
    }


async def evaluate_live(
    dataset: Dataset, output: Path, provider, config: LiveConfig, *, split="dev", scenario_id=None
) -> dict:
    validate_live_scope(dataset, config, split, scenario_id)
    scenarios = [s for s in dataset.select(split, scenario_id) if "routing" in s.tags]
    if not scenarios:
        raise ValueError("No routing scenarios selected")
    revision = prepare_output(output)
    metadata = {
        "evaluation_type": "Real Model Evaluation",
        "split": split,
        "scenario_id": scenario_id,
        "selected_scenario_ids": [s.scenario_id for s in scenarios],
        "live_config": config.model_dump(mode="json"),
        "hashes": {**dataset.hashes, "live_config_hash": digest(config.model_dump(mode="json"))},
        "git_commit": revision.stdout.strip() if revision.returncode == 0 else None,
        "git_dirty": working_tree_dirty(),
        "code_hash": evaluation_code_hash(),
    }
    write(output / "live-metadata.json", metadata)
    trials = []
    metered = MeteredProvider(provider, config)

    def progress():
        write(
            output / "live-progress.json",
            {
                "calls": metered.calls,
                "trials": [t.model_dump(mode="json") for t in trials],
                "usage": metered.usage(),
                "stop_reason": metered.stopped,
            },
        )

    metered.on_change = progress
    try:
        async with asyncio.timeout(config.timeout_seconds):
            for repetition in range(1, config.repetitions + 1):
                for variant in ("rule", "model-assisted"):
                    for scenario in scenarios:
                        adapter = (
                            LiveRoutingAdapter(metered, repetition)
                            if variant == "model-assisted"
                            else RoutingAdapter()
                        )
                        result = await replay(scenario, adapter=adapter)
                        trials.append(Trial(variant=variant, repetition=repetition, result=result))
                        progress()
                        if result.status != "completed":
                            raise LiveStopped(result.error_code)
        bundle = LiveBundle(
            version=live_version(config),
            corpus=dataset.corpus,
            gold=dataset.gold,
            splits=dataset.splits,
            config=dataset.config,
            protocol=dataset.protocol,
            live_config=config,
            split=split,
            scenario_id=scenario_id,
            hashes={**dataset.hashes, "live_config_hash": digest(config.model_dump(mode="json"))},
            git_commit=metadata["git_commit"],
            git_dirty=metadata["git_dirty"],
            code_hash=metadata["code_hash"],
            trials=trials,
            calls=metered.calls,
            usage=metered.usage(),
        )
        report = live_summary(bundle)
        payload = bundle.model_dump(mode="json")
        write(output / "live-trace.json", payload)
        write(output / "trace-hash.json", {"sha256": digest(payload)})
        write(output / "summary.json", report)
        write(output / "status.json", {"status": "completed"})
        return report
    except BaseException as error:
        progress()
        write(
            output / "status.json",
            {
                "status": "cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                "error_code": metered.stopped or type(error).__name__,
                "usage": metered.usage(),
            },
        )
        raise


def validate_live_scope(dataset: Dataset, config: LiveConfig, split: str, scenario_id: str | None):
    if isinstance(config, NoThinkingDevConfig) and (
        config.model != "deepseek-flash" or config.endpoint_host != "api.deepseek.com"
    ):
        raise ValueError("Non-thinking profile requires the verified DeepSeek model and endpoint")
    if isinstance(config, DevLiveConfig):
        if (
            split != "dev"
            or scenario_id is not None
            or dataset.protocol is None
            or dataset.protocol.version != "routing-v1-draft1"
            or [s.scenario_id for s in dataset.select("dev")] != [f"LH{n:03d}" for n in range(1, 9)]
            or any(len(s.turns) != 18 for s in dataset.select("dev"))
        ):
            raise ValueError("routing-dev-v1 requires the complete frozen v1 dev split only")


def recompute_live(output: Path) -> dict:
    import json

    require_private_directory(output)
    if json.loads((output / "status.json").read_text(encoding="utf-8")) != {"status": "completed"}:
        raise ValueError("Live result incomplete")
    raw = (output / "live-trace.json").read_text(encoding="utf-8")
    expected = json.loads((output / "trace-hash.json").read_text(encoding="utf-8"))["sha256"]
    if digest(json.loads(raw)) != expected:
        raise ValueError("Live trace hash mismatch")
    report = live_summary(LiveBundle.model_validate_json(raw))
    write(output / "summary.json", report)
    return report
