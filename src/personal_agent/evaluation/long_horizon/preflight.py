"""Offline budget paths through the real Router; scripted choices are not predictions."""

import json

from personal_agent.core.budget import estimate_request
from personal_agent.core.models import ModelResponse
from personal_agent.evaluation.long_horizon.dataset import Dataset
from personal_agent.evaluation.long_horizon.replay import replay
from personal_agent.evaluation.long_horizon.routing_eval import RoutingAdapter


class BudgetProbe:
    def __init__(self, action: str):
        self.action = action
        self.input_estimates: list[int] = []

    async def generate(self, messages, tools, *, max_output_tokens=None, **options):
        if tools or max_output_tokens != 1024:
            raise ValueError("Unexpected routing request")
        self.input_estimates.append(estimate_request(messages, tools))
        return ModelResponse(text=json.dumps({"action": self.action}))


async def preflight(dataset: Dataset, *, split="dev", scenario_id=None, input_cap=4096) -> dict:
    if input_cap < 1:
        raise ValueError("Planning input cap must be positive")
    scenarios = [s for s in dataset.select(split, scenario_id) if "routing" in s.tags]
    if not scenarios:
        raise ValueError("No routing scenarios")
    rows = []
    for action in ("continue", "clarify", "new"):
        counts = []
        for scenario in scenarios:
            provider = BudgetProbe(action)
            result = await replay(
                scenario, adapter=RoutingAdapter(provider=provider, model_selection=True)
            )
            result.validate_for(scenario)
            counts.append(
                {
                    "scenario_id": scenario.scenario_id,
                    "calls": len(provider.input_estimates),
                    "estimated_input_tokens": sum(provider.input_estimates),
                    "max_estimated_input_tokens": max(provider.input_estimates, default=0),
                }
            )
        rows.append(
            {
                "scripted_action": action,
                "scenarios": counts,
                "calls": sum(c["calls"] for c in counts),
                "estimated_input_tokens": sum(c["estimated_input_tokens"] for c in counts),
                "max_estimated_input_tokens": max(c["max_estimated_input_tokens"] for c in counts),
            }
        )
    # Each fresh Scenario has no candidate on its first turn; each later turn can call once.
    upper = sum(len(s.turns) - 1 for s in scenarios)
    return {
        "evaluation_type": "Offline Contract Test",
        "purpose": "budget paths, not model quality",
        "status": "completed",
        "split": split,
        **dataset.hashes,
        "scripted_paths": rows,
        "planning_input_cap": input_cap,
        "observed_input_cap_exceeded": any(
            r["max_estimated_input_tokens"] > input_cap for r in rows
        ),
        "single_pass_request_upper_bound": upper,
        "single_pass_reserved_tokens_at_cap": upper * (input_cap + 1024),
        "actual_network_requests": 0,
        "usage": "unknown; scripted providers do not return model usage",
        "limitations": (
            "Actual model choices may produce different paths; estimates are not tokenizer counts"
        ),
    }
