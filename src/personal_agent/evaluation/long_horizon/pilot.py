"""Frozen three-scenario v2 pilot: one metered root across routing and real maintenance."""

import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from personal_agent.evaluation.long_horizon.ablation import RoutingProbe
from personal_agent.evaluation.long_horizon.dataset import Dataset, digest
from personal_agent.evaluation.long_horizon.live import LiveConfig, LiveStopped, MeteredProvider
from personal_agent.evaluation.long_horizon.metrics import SCORER_VERSION, aggregate, score
from personal_agent.evaluation.long_horizon.models import ReplayResult, Schema
from personal_agent.evaluation.long_horizon.policies import FEWSHOT, IDENTITY_POLICY, PolicyProvider
from personal_agent.evaluation.long_horizon.replay import replay
from personal_agent.evaluation.long_horizon.report import (
    Bundle,
    evaluation_code_hash,
    prepare_output,
    working_tree_dirty,
    write,
)
from personal_agent.evaluation.long_horizon.routing_eval import RoutingAdapter
from personal_agent.evaluation.long_horizon.state import (
    ExtractiveContractProvider,
    ProductionMaintenance,
)
from personal_agent.memory.privacy import require_private_directory

SCENARIOS = ["LH019", "LH021", "LH022"]
ARMS = ["baseline", "state", "identity", "fewshot"]
INTERVAL = 6


class PilotConfig(LiveConfig):
    profile: Literal["routing-pilot-v2"] = "routing-pilot-v2"
    model: Literal["deepseek-flash"]
    endpoint_host: Literal["api.deepseek.com"]
    repetitions: Literal[1] = 1
    reasoning_effort: Literal["none"] = "none"
    max_requests: int = Field(default=400, ge=1, le=400)
    max_tokens: int = Field(default=2_000_000, ge=1, le=2_000_000)
    max_input_tokens: Literal[32768] = 32768
    timeout_seconds: Literal[3600] = 3600

    def request_limits(self, stage: str) -> tuple[int, int]:
        if stage == "routing":
            return 8192, 1024
        if stage in {"episode", "thread"}:
            return 32768, 4096
        raise ValueError("Unknown pilot stage")


class JsonPilotConfig(PilotConfig):
    profile: Literal["routing-pilot-v2-json"] = "routing-pilot-v2-json"
    json_mode: Literal[True] = True
    prior_reserved_cost_cny: float = Field(gt=0, le=10, allow_inf_nan=False)
    prior_reserved_tokens: int = Field(ge=1, le=2_000_000)
    prior_requests: int = Field(ge=1, le=400)
    prior_attempt_hash: str = Field(pattern="^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def shared_attempt_budget(self):
        if (
            self.max_cost_cny + self.prior_reserved_cost_cny > 10 + 1e-9
            or self.max_tokens + self.prior_reserved_tokens > 2_000_000
            or self.max_requests + self.prior_requests > 400
        ):
            raise ValueError("Combined pilot attempts exceed the authorized root budget")
        return self


def prior_attempt(path: Path, data: Dataset) -> dict:
    """Seal the failed attempt and carry its full reservations, never just its cheaper usage."""
    require_private_directory(path)
    saved = {
        name: json.loads((path / f"pilot-{name}.json").read_text(encoding="utf-8"))
        for name in ("metadata", "progress")
    }
    saved["status"] = json.loads((path / "status.json").read_text(encoding="utf-8"))
    metadata, progress, status = saved["metadata"], saved["progress"], saved["status"]
    if (
        status["status"] != "failed"
        or metadata["hashes"] != data.hashes
        or metadata["pilot_config"]["profile"] != "routing-pilot-v2"
        or metadata["scenario_ids"] != SCENARIOS
        or metadata["arms"] != ARMS
        or status["usage"] != progress["usage"]
        or not status["usage"]["usage_complete"]
    ):
        raise ValueError("Prior pilot cannot establish a complete shared budget ledger")
    old_config = PilotConfig.model_validate(metadata["pilot_config"])
    totals = usage_for(progress["calls"], old_config)
    reserved_tokens = sum(
        c["estimated_input_tokens"] + c["output_limit"] for c in progress["calls"]
    )
    reserved_cost = (
        sum(
            c["estimated_input_tokens"] * old_config.input_price_cny
            + c["output_limit"] * old_config.output_price_cny
            for c in progress["calls"]
        )
        / 1_000_000
    )
    if (
        totals["actual_requests"] != status["usage"]["actual_requests"]
        or reserved_tokens != status["usage"]["reserved_tokens"]
        or abs(reserved_cost - status["usage"]["reserved_cost_cny"]) > 1e-9
        or any(
            totals[k] != status["usage"][k]
            for k in ("input_tokens", "output_tokens", "cost_cny_upper_estimate")
        )
    ):
        raise ValueError("Prior pilot budget accounting mismatch")
    return {
        "prior_reserved_cost_cny": reserved_cost,
        "prior_reserved_tokens": reserved_tokens,
        "prior_requests": totals["actual_requests"],
        "prior_attempt_hash": digest(saved),
    }


def protocol() -> dict:
    return json.loads(Path(__file__).with_name("pilot_protocol.json").read_text(encoding="utf-8"))


def validate_scope(data: Dataset, plan: dict, split="dev", scenario_id=None) -> list:
    if (
        split != "dev"
        or scenario_id is not None
        or plan["version"] != "routing-v2-pilot-1"
        or plan["scenario_ids"] != SCENARIOS
        or plan["arms"] != ARMS
        or plan["maintenance_interval"] != INTERVAL
        or plan["dataset_hashes"] != data.hashes
        or not set(SCENARIOS) <= {s.scenario_id for s in data.select("dev")}
    ):
        raise ValueError("Pilot requires frozen v2 and its three authorized dev scenarios")
    return [next(s for s in data.corpus.scenarios if s.scenario_id == sid) for sid in SCENARIOS]


class StageProvider:
    def __init__(self, root: MeteredProvider):
        self.root = root

    async def generate(self, messages, tools, **options):
        data = json.loads(messages[1].text)
        self.root.scope["stage"] = "episode" if "runs" in data else "thread"
        response = await self.root.generate(messages, tools, **options)
        if self.root.stopped:
            raise LiveStopped(self.root.stopped)
        return response


class PilotMaintenance(ProductionMaintenance):
    def __init__(self, root: MeteredProvider, arm: str, scenario_id: str):
        super().__init__(
            StageProvider(root), interval=INTERVAL, context_window=root.config.context_window
        )
        self.root, self.arm, self.scenario_id = root, arm, scenario_id

    async def after_completed(self, repository, turn, count):
        self.root.scope = dict(arm=self.arm, scenario_id=self.scenario_id, turn_id=turn.turn_id)
        return await super().after_completed(repository, turn, count)


class PilotAdapter(RoutingAdapter):
    def __init__(self, root: MeteredProvider, arm: str):
        policy = arm if arm in {"identity", "fewshot"} else "baseline"
        super().__init__(provider=PolicyProvider(root, policy), model_selection=True)
        self.root, self.arm = root, arm

    async def route(self, repository, run_id, turn, scenario_id):
        self.root.scope = dict(
            arm=self.arm, scenario_id=scenario_id, turn_id=turn.turn_id, stage="routing"
        )
        before = len(self.root.calls)
        prediction = await super().route(repository, run_id, turn, scenario_id)
        if self.root.stopped == "CancelledError":
            self.root.stopped = "router_timeout"
            self.root.calls[-1]["error_code"] = "router_timeout"
        if self.root.stopped:
            raise LiveStopped(self.root.stopped)
        calls = self.root.calls[before:]
        usage = calls[0]["usage"] if calls else None
        return prediction.model_copy(
            update={
                "input_tokens": usage["input_tokens"] if usage else None,
                "output_tokens": usage["output_tokens"] if usage else None,
            }
        )


class PilotTrial(Schema):
    arm: Literal["baseline", "state", "identity", "fewshot"]
    result: ReplayResult


class PilotBundle(Schema):
    version: Literal["live-pilot-v2-1", "live-pilot-v2-json-1"] = "live-pilot-v2-1"
    dataset: Bundle
    pilot_config: JsonPilotConfig | PilotConfig
    pilot_protocol: dict
    identity_policy: str = IDENTITY_POLICY
    fewshot: str = FEWSHOT
    trials: list[PilotTrial]
    calls: list[dict]
    usage: dict


def usage_for(calls: list[dict], config: PilotConfig) -> dict:
    if any(c["status"] != "completed" or c["usage"] is None for c in calls):
        raise ValueError("Pilot calls or usage incomplete")
    inputs = sum(c["usage"]["input_tokens"] for c in calls)
    outputs = sum(c["usage"]["output_tokens"] for c in calls)
    return {
        "actual_requests": len(calls),
        "input_tokens": inputs,
        "output_tokens": outputs,
        "reasoning_tokens": sum(c["usage"].get("reasoning_tokens") or 0 for c in calls),
        "cost_cny_upper_estimate": (
            inputs * config.input_price_cny + outputs * config.output_price_cny
        )
        / 1_000_000,
    }


def summarize(bundle: PilotBundle) -> dict:
    saved = bundle.dataset
    data = Dataset(saved.corpus, saved.gold, saved.splits, saved.config, saved.protocol)
    scenarios = validate_scope(data, bundle.pilot_protocol)
    if saved.hashes != data.hashes or saved.results:
        raise ValueError("Pilot dataset provenance mismatch")
    if [(t.arm, t.result.scenario_id) for t in bundle.trials] != [
        (arm, s.scenario_id) for arm in ARMS for s in scenarios
    ]:
        raise ValueError("Pilot trial coverage incomplete")
    config = bundle.pilot_config
    if bundle.version != (
        "live-pilot-v2-json-1" if isinstance(config, JsonPilotConfig) else "live-pilot-v2-1"
    ):
        raise ValueError("Pilot output format version mismatch")
    totals = usage_for(bundle.calls, config)
    for key in ("actual_requests", "input_tokens", "output_tokens", "cost_cny_upper_estimate"):
        if abs(bundle.usage[key] - totals[key]) > 1e-9:
            raise ValueError("Pilot usage mismatch")
    reserved_tokens = sum(c["estimated_input_tokens"] + c["output_limit"] for c in bundle.calls)
    reserved_cost = (
        sum(
            c["estimated_input_tokens"] * config.input_price_cny
            + c["output_limit"] * config.output_price_cny
            for c in bundle.calls
        )
        / 1_000_000
    )
    if (
        not bundle.usage["usage_complete"]
        or bundle.usage["reserved_tokens"] != reserved_tokens
        or abs(bundle.usage["reserved_cost_cny"] - reserved_cost) > 1e-9
        or totals["actual_requests"] > config.max_requests
        or reserved_tokens > config.max_tokens
        or reserved_cost > config.max_cost_cny
        or totals["input_tokens"] + totals["output_tokens"] > config.max_tokens
        or totals["cost_cny_upper_estimate"] > config.max_cost_cny
    ):
        raise ValueError("Pilot shared budget mismatch")
    known = {(t.arm, t.result.scenario_id): t for t in bundle.trials}
    for c in bundle.calls:
        trial = known.get((c["arm"], c["scenario_id"]))
        if trial is None or c["turn_id"] not in {p.turn_id for p in trial.result.predictions}:
            raise ValueError("Pilot call scope mismatch")
        cap, output = config.request_limits(c["stage"])
        if (
            c["output_limit"] != output
            or c["estimated_input_tokens"] > cap
            or c["usage"]["input_tokens"] > cap
            or c["usage"]["output_tokens"] > output
            or c["request_options"] != {"max_output_tokens": output, "reasoning_effort": "none"}
            or c["prompt_hash"] != digest(c["messages"][0]["text"])
            or c.get("output_format")
            != ("json_object" if isinstance(config, JsonPilotConfig) else None)
        ):
            raise ValueError("Pilot call limits/options mismatch")
    arms = {}
    for arm in ARMS:
        trials = [t for t in bundle.trials if t.arm == arm]
        rows = []
        for t, scenario in zip(trials, scenarios, strict=True):
            t.result.validate_for(scenario)
            calls = [
                c
                for c in bundle.calls
                if c["arm"] == arm and c["scenario_id"] == scenario.scenario_id
            ]
            routing = [c for c in calls if c["stage"] == "routing"]
            keys = [c["turn_id"] for c in routing]
            if len(keys) != len(set(keys)) or {
                p.turn_id for p in t.result.predictions if p.request_count == 1
            } != set(keys):
                raise ValueError("Pilot routing accounting mismatch")
            recorded = [c for m in t.result.maintenance for c in m["calls"]]
            actual = [c for c in calls if c["stage"] != "routing"]
            if len(recorded) != len(actual) or any(
                any(
                    a[k] != r[k]
                    for k in ("stage", "status", "estimated_input_tokens", "output_limit")
                )
                for a, r in zip(actual, recorded, strict=True)
            ):
                raise ValueError("Pilot maintenance accounting mismatch")
            expected = (
                []
                if arm == "baseline"
                else [t.turn_id for t in scenario.turns[INTERVAL - 1 :: INTERVAL]]
            )
            if [m["after_turn_id"] for m in t.result.maintenance] != expected:
                raise ValueError("Pilot maintenance coverage mismatch")
            rows.append(score(t.result.predictions, data.gold_for(scenario.scenario_id)))
        calls = [c for c in bundle.calls if c["arm"] == arm]
        arms[arm] = {
            "scenario_metrics": {s.scenario_id: r for s, r in zip(scenarios, rows, strict=True)},
            "aggregate": aggregate(rows),
            "completed_turns": sum(len(t.result.predictions) for t in trials),
            "usage_by_stage": {
                stage: usage_for([c for c in calls if c["stage"] == stage], config)
                for stage in ("routing", "episode", "thread")
            },
            "prompt_hashes": sorted({c["prompt_hash"] for c in calls if c["stage"] == "routing"}),
            "routing_reasons": dict(
                Counter(p.routing_reason for t in trials for p in t.result.predictions)
            ),
            "state_semantic_accuracy": None,
        }
    return {
        "evaluation_type": "Real Model Evaluation",
        "status": "completed",
        "purpose": (
            "three-scenario paired pilot; provisional gold, no independent generalization claim"
        ),
        "annotation_status": "agent-authored provisional gold",
        "scorer_version": SCORER_VERSION,
        "pilot_config": config.model_dump(mode="json"),
        "pilot_config_hash": digest(config.model_dump(mode="json")),
        "pilot_protocol_hash": digest(bundle.pilot_protocol),
        "identity_policy_hash": digest(bundle.identity_policy),
        "fewshot_hash": digest(bundle.fewshot),
        **data.hashes,
        "git_commit": saved.git_commit,
        "git_dirty": saved.git_dirty,
        "code_hash": saved.code_hash,
        "scenario_ids": SCENARIOS,
        "maintenance_interval": INTERVAL,
        "holdout_evaluated": False,
        "arms": arms,
        "usage": {**bundle.usage, "reasoning_tokens": totals["reasoning_tokens"]},
    }


async def evaluate_pilot(
    data: Dataset, output: Path, provider, config: PilotConfig, *, split="dev", scenario_id=None
) -> dict:
    plan = protocol()
    scenarios = validate_scope(data, plan, split, scenario_id)
    revision = prepare_output(output)
    saved = Bundle(
        corpus=data.corpus,
        gold=data.gold,
        splits=data.splits,
        config=data.config,
        protocol=data.protocol,
        split="dev",
        scenario_id=None,
        git_commit=revision.stdout.strip() if revision.returncode == 0 else None,
        git_dirty=working_tree_dirty(),
        code_hash=evaluation_code_hash(),
        hashes=data.hashes,
        results=[],
    )
    write(
        output / "pilot-metadata.json",
        {
            "evaluation_type": "Real Model Evaluation",
            "scenario_ids": SCENARIOS,
            "arms": ARMS,
            "pilot_config": config.model_dump(mode="json"),
            "pilot_protocol": plan,
            "pilot_protocol_hash": digest(plan),
            "hashes": data.hashes,
            "git_commit": saved.git_commit,
            "git_dirty": saved.git_dirty,
            "code_hash": saved.code_hash,
            "identity_policy": IDENTITY_POLICY,
            "fewshot": FEWSHOT,
        },
    )
    root = MeteredProvider(provider, config)
    trials = []

    def progress():
        write(
            output / "pilot-progress.json",
            {
                "calls": root.calls,
                "trials": [t.model_dump(mode="json") for t in trials],
                "usage": root.usage(),
                "stop_reason": root.stopped,
            },
        )

    root.on_change = progress
    try:
        async with asyncio.timeout(config.timeout_seconds):
            for arm in ARMS:
                for scenario in scenarios:
                    maintenance = (
                        PilotMaintenance(root, arm, scenario.scenario_id)
                        if arm != "baseline"
                        else None
                    )
                    result = await replay(
                        scenario, adapter=PilotAdapter(root, arm), maintenance=maintenance
                    )
                    trials.append(PilotTrial(arm=arm, result=result))
                    progress()
                    if result.status != "completed" or root.stopped:
                        raise LiveStopped(root.stopped or result.error_code)
        bundle = PilotBundle(
            version="live-pilot-v2-json-1"
            if isinstance(config, JsonPilotConfig)
            else "live-pilot-v2-1",
            dataset=saved,
            pilot_config=config,
            pilot_protocol=plan,
            trials=trials,
            calls=root.calls,
            usage=root.usage(),
        )
        report = summarize(bundle)
        payload = bundle.model_dump(mode="json")
        write(output / "pilot-trace.json", payload)
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
                "error_code": root.stopped or type(error).__name__,
                "usage": root.usage(),
                "completed_trials": [
                    {"arm": t.arm, "scenario_id": t.result.scenario_id}
                    for t in trials
                    if t.result.status == "completed"
                ],
            },
        )
        raise


def recompute_pilot(output: Path) -> dict:
    require_private_directory(output)
    if json.loads((output / "status.json").read_text(encoding="utf-8")) != {"status": "completed"}:
        raise ValueError("Pilot incomplete")
    raw = (output / "pilot-trace.json").read_text(encoding="utf-8")
    if (
        digest(json.loads(raw))
        != json.loads((output / "trace-hash.json").read_text(encoding="utf-8"))["sha256"]
    ):
        raise ValueError("Pilot trace hash mismatch")
    report = summarize(PilotBundle.model_validate_json(raw))
    write(output / "summary.json", report)
    return report


async def preflight(data: Dataset) -> dict:
    scenarios = validate_scope(data, protocol())
    paths = {}
    for action in ("continue", "clarify", "new"):
        arms = {}
        for arm in ARMS:
            calls = []
            for scenario in scenarios:
                probe = RoutingProbe(action)
                policy = arm if arm in {"identity", "fewshot"} else "baseline"
                maintenance = (
                    ProductionMaintenance(ExtractiveContractProvider(), interval=INTERVAL)
                    if arm != "baseline"
                    else None
                )
                result = await replay(
                    scenario,
                    adapter=RoutingAdapter(
                        provider=PolicyProvider(probe, policy), model_selection=True
                    ),
                    maintenance=maintenance,
                )
                result.validate_for(scenario)
                calls.extend([*probe.calls, *(maintenance.calls if maintenance else [])])
            arms[arm] = {
                "synthetic_calls": len(calls),
                "estimated_input_tokens": sum(c["estimated_input_tokens"] for c in calls),
                "reserved_output_tokens": sum(c["output_limit"] for c in calls),
                "stages": {
                    stage: {
                        "synthetic_calls": sum(c["stage"] == stage for c in calls),
                        "max_estimated_input_tokens": max(
                            (c["estimated_input_tokens"] for c in calls if c["stage"] == stage),
                            default=0,
                        ),
                    }
                    for stage in ("routing", "episode", "thread")
                },
            }
        paths[action] = arms
    return {
        "evaluation_type": "Offline Contract Test",
        "status": "completed",
        "scenario_ids": SCENARIOS,
        "maintenance_interval": INTERVAL,
        "actual_network_requests": 0,
        "token_usage": "unknown",
        "model_quality_metrics": None,
        "pilot_protocol_hash": digest(protocol()),
        **data.hashes,
        "paths": paths,
    }
