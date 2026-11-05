"""Offline state/prompt/rule ablations: mechanism and cost paths, not model efficacy."""

import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import Field

from personal_agent.core.budget import estimate_request
from personal_agent.core.models import ModelResponse
from personal_agent.evaluation.long_horizon.dataset import Corpus, Dataset, Gold, Splits, digest
from personal_agent.evaluation.long_horizon.models import (
    BenchmarkProtocol,
    Config,
    ReplayResult,
    Schema,
)
from personal_agent.evaluation.long_horizon.policies import FEWSHOT, IDENTITY_POLICY, PolicyProvider
from personal_agent.evaluation.long_horizon.replay import replay
from personal_agent.evaluation.long_horizon.report import (
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


class AblationConfig(Schema):
    name: Literal["baseline", "state", "identity", "fewshot", "deferred"]
    state_mode: Literal["off", "extractive-contract"] = "off"
    prompt_policy: Literal["baseline", "identity", "fewshot"] = "baseline"
    defer_weak_rules: bool = False
    maintenance_interval: int = Field(default=3, ge=1, le=16)
    scripted_action: Literal["continue", "clarify", "new"] = "continue"


def preset(name: str, action: str = "continue") -> AblationConfig:
    return AblationConfig(
        name=name,
        state_mode="off" if name == "baseline" else "extractive-contract",
        prompt_policy="identity"
        if name == "identity"
        else "fewshot"
        if name in {"fewshot", "deferred"}
        else "baseline",
        defer_weak_rules=name == "deferred",
        scripted_action=action,
    )


class RoutingProbe:
    def __init__(self, action: str):
        self.action = action
        self.calls: list[dict] = []

    async def generate(self, messages, tools, *, max_output_tokens=None, **options):
        if tools or max_output_tokens != 1024:
            raise ValueError("Unexpected routing request")
        data = json.loads(messages[1].text)
        self.calls.append(
            {
                "estimated_input_tokens": estimate_request(messages, tools),
                "output_limit": max_output_tokens,
                "stage": "routing",
                "status": "completed",
                "card_count": len(data["threads"]),
                "nonempty_state_cards": sum(bool(t["state"]) for t in data["threads"]),
                "prompt_hash": digest(messages[0].text),
                "messages": [m.model_dump(mode="json") for m in messages],
            }
        )
        return ModelResponse(text=json.dumps({"action": self.action}))


class AblationTrial(Schema):
    config: AblationConfig
    result: ReplayResult
    calls: list[dict]


class AblationBundle(Schema):
    version: Literal["ablation-contract-1"] = "ablation-contract-1"
    corpus: Corpus
    gold: Gold
    splits: Splits
    config: Config
    protocol: BenchmarkProtocol | None = None
    split: Literal["dev"] = "dev"
    scenario_id: str | None
    arm_names: list[str]
    scripted_action: str
    hashes: dict[str, str]
    git_commit: str | None
    git_dirty: bool
    code_hash: str
    trials: list[AblationTrial]
    identity_policy: str = IDENTITY_POLICY
    fewshot: str = FEWSHOT


def summarize(bundle: AblationBundle) -> dict:
    data = Dataset(bundle.corpus, bundle.gold, bundle.splits, bundle.config, bundle.protocol)
    if bundle.hashes != data.hashes:
        raise ValueError("Ablation provenance mismatch")
    scenarios = data.select(bundle.split, bundle.scenario_id)
    if [(t.config.name, t.result.scenario_id) for t in bundle.trials] != [
        (name, s.scenario_id) for name in bundle.arm_names for s in scenarios
    ]:
        raise ValueError("Ablation coverage incomplete")
    arms = {}
    for name in bundle.arm_names:
        trials = [t for t in bundle.trials if t.config.name == name]
        expected = preset(name, bundle.scripted_action)
        calls = []
        for trial, scenario in zip(trials, scenarios, strict=True):
            if trial.config != expected:
                raise ValueError("Ablation configuration mismatch")
            trial.result.validate_for(scenario)
            calls.extend(trial.calls)
        stages = {
            stage: {
                "synthetic_calls": sum(c["stage"] == stage for c in calls),
                "estimated_input_tokens": sum(
                    c["estimated_input_tokens"] for c in calls if c["stage"] == stage
                ),
                "reserved_output_tokens": sum(
                    c["output_limit"] for c in calls if c["stage"] == stage
                ),
                "max_estimated_input_tokens": max(
                    (c["estimated_input_tokens"] for c in calls if c["stage"] == stage), default=0
                ),
            }
            for stage in ("routing", "episode", "thread")
        }
        if any(c["status"] != "completed" for c in calls):
            raise ValueError("Incomplete synthetic call")
        routing = [c for c in calls if c["stage"] == "routing"]
        arms[name] = {
            "configuration": expected.model_dump(mode="json"),
            "configuration_hash": digest(expected.model_dump(mode="json")),
            "completed_turns": sum(len(t.result.predictions) for t in trials),
            "maintenance_windows": sum(len(t.result.maintenance) for t in trials),
            "stages": stages,
            "supplied_thread_cards": sum(c["card_count"] for c in routing),
            "cards_with_state": sum(c["nonempty_state_cards"] for c in routing),
            "prompt_hashes": sorted({c["prompt_hash"] for c in routing}),
            "routing_reasons": dict(
                Counter(p.routing_reason for t in trials for p in t.result.predictions)
            ),
            "model_quality_metrics": None,
        }
    return {
        "evaluation_type": "Offline Contract Test",
        "status": "completed",
        "purpose": "mechanism and budget paths; scripted choices do not measure policy efficacy",
        "annotation_status": "agent-authored provisional gold",
        **bundle.hashes,
        "git_commit": bundle.git_commit,
        "git_dirty": bundle.git_dirty,
        "code_hash": bundle.code_hash,
        "split": bundle.split,
        "scenario_support": len(scenarios),
        "arms": arms,
        "identity_policy_hash": digest(bundle.identity_policy),
        "fewshot_hash": digest(bundle.fewshot),
        "actual_network_requests": 0,
        "token_usage": "unknown",
    }


async def evaluate_ablation(
    data: Dataset, output: Path, *, arm="all", split="dev", scenario_id=None, action="continue"
) -> dict:
    if split != "dev":
        raise ValueError("Ablation development stage does not run Holdout")
    names = ["baseline", "state", "identity", "fewshot", "deferred"] if arm == "all" else [arm]
    configs = [preset(n, action) for n in names]
    scenarios = data.select(split, scenario_id)
    revision = prepare_output(output)
    trials = []
    try:
        for config in configs:
            for scenario in scenarios:
                probe = RoutingProbe(config.scripted_action)
                maintenance = (
                    ProductionMaintenance(
                        ExtractiveContractProvider(), interval=config.maintenance_interval
                    )
                    if config.state_mode != "off"
                    else None
                )
                adapter = RoutingAdapter(
                    provider=PolicyProvider(probe, config.prompt_policy),
                    model_selection=True,
                    defer_weak_rules=config.defer_weak_rules,
                )
                result = await replay(scenario, adapter=adapter, maintenance=maintenance)
                trials.append(
                    AblationTrial(
                        config=config,
                        result=result,
                        calls=[*probe.calls, *(maintenance.calls if maintenance else [])],
                    )
                )
                write(
                    output / "ablation-progress.json", [t.model_dump(mode="json") for t in trials]
                )
                result.validate_for(scenario)
        bundle = AblationBundle(
            corpus=data.corpus,
            gold=data.gold,
            splits=data.splits,
            config=data.config,
            protocol=data.protocol,
            scenario_id=scenario_id,
            arm_names=names,
            scripted_action=action,
            hashes=data.hashes,
            git_commit=revision.stdout.strip() if revision.returncode == 0 else None,
            git_dirty=working_tree_dirty(),
            code_hash=evaluation_code_hash(),
            trials=trials,
        )
        report = summarize(bundle)
        payload = bundle.model_dump(mode="json")
        write(output / "ablation-trace.json", payload)
        write(output / "trace-hash.json", {"sha256": digest(payload)})
        write(output / "summary.json", report)
        write(output / "status.json", {"status": "completed"})
        return report
    except BaseException as error:
        write(
            output / "status.json",
            {
                "status": "cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                "error_code": type(error).__name__,
            },
        )
        raise


def recompute_ablation(output: Path) -> dict:
    require_private_directory(output)
    if json.loads((output / "status.json").read_text(encoding="utf-8")) != {"status": "completed"}:
        raise ValueError("Ablation incomplete")
    raw = (output / "ablation-trace.json").read_text(encoding="utf-8")
    expected = json.loads((output / "trace-hash.json").read_text(encoding="utf-8"))["sha256"]
    if digest(json.loads(raw)) != expected:
        raise ValueError("Ablation trace hash mismatch")
    report = summarize(AblationBundle.model_validate_json(raw))
    write(output / "summary.json", report)
    return report
