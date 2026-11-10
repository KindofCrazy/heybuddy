"""Persistent long trajectories; Gold is absent from inference and no real channels exist."""

import asyncio
import tempfile
from datetime import datetime
from pathlib import Path

from personal_agent.core.budget import ContextBudget
from personal_agent.evaluation.heybuddy_bench.adapters import FixedClock, memory_contents, seed_run
from personal_agent.evaluation.heybuddy_bench.runner import Meter
from personal_agent.evaluation.heybuddy_bench.sequence_replay import (
    Observer,
    SequenceContractProvider,
    learning_service,
    proactive_step,
    replay,
    route_step,
    seed_threads,
)
from personal_agent.organization.models import OrganizationPolicy
from personal_agent.organization.provider import OrganizerModel
from personal_agent.proactive.models import ProactivePolicy
from personal_agent.proactive.runtime import ProactiveRuntime
from personal_agent.sessions import SessionManager
from personal_agent.storage.proactive import ProactiveRepository
from personal_agent.storage.sqlite import SQLiteStore


async def replay_long(case, root, provider, observed, on_step):
    body, track = case["input"], case["track"]
    clock = FixedClock(body["steps"][0]["at"])
    with SQLiteStore(root / "long.sqlite3") as store:
        store.initialize()
        sessions = SessionManager(store)
        if track == "routing":
            repo = seed_threads(store, sessions, body)
        elif track == "memory":
            memory, learner = learning_service(
                store, sessions, root, clock, provider, body["memory_files"]
            )
        else:
            policy = ProactivePolicy(compose_enabled=False, social_followup_enabled=False)
            runtime = ProactiveRuntime(
                ProactiveRepository(store, sessions.agent.id),
                OrganizerModel(
                    provider,
                    ContextBudget(context_window=131072),
                    OrganizationPolicy(
                        input_tokens=policy.input_tokens, output_tokens=policy.output_tokens
                    ),
                ),
                policy,
                clock=clock,
            )
        pending, run_ids = [], {}
        for step in body["steps"]:
            clock.at = datetime.fromisoformat(step["at"])
            if track == "routing":
                result = await route_step(store, sessions, repo, step, provider)
            elif track == "memory":
                events = [dict(text=step["text"], timestamp=step["at"])]
                if step.get("assistant"):
                    events.append(
                        dict(text=step["assistant"], timestamp=step["at"], speaker="assistant")
                    )
                pending.append(seed_run(store, sessions.current.id, events))
                learner.observed_decisions = []
                if step["learn"]:
                    outcome = await learner.learn(run_ids=pending)
                    pending = []  # New observations only; never blindly retry failed old jobs.
                else:
                    outcome = dict(outcome="not_requested")
                result = dict(
                    files=memory_contents(memory),
                    outcome=outcome,
                    error_code=outcome.get("error_code")
                    or (
                        outcome["outcome"]
                        if outcome["outcome"] not in {"completed", "not_requested"}
                        else None
                    ),
                )
            else:
                result = await proactive_step(runtime, store, sessions, step, run_ids)
                state = runtime.repository.state()
                if state and state["error_code"]:
                    result["error_code"] = state["error_code"]
            result["step_id"] = step["id"]
            observed.append(result)
            if on_step:
                on_step(case["case_id"], step["id"], result.get("error_code"))
            # Semantic/execution errors remain failures; later NEW inputs can expose recovery.
            # Unknown billing or an exhausted global budget forbids all further model calls.
            if provider.meter.budget.exhausted or (
                not provider.meter.offline and not provider.meter.budget.usage_complete
            ):
                break


async def run_cases(
    cases, *, temp_parent: Path, budget, provider=None, short=False, on_result=None, on_step=None
):
    temp_parent.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    meter = Meter(provider or SequenceContractProvider(), budget, offline=provider is None)
    observer = Observer(meter)
    results = []
    for case in cases:
        observed, error, start = [], None, len(meter.calls)
        try:
            async with asyncio.timeout(1200):
                with tempfile.TemporaryDirectory(prefix="hb-long-", dir=temp_parent) as name:
                    if short:
                        await replay(case, Path(name), observer, observed)
                    else:
                        await replay_long(case, Path(name), observer, observed, on_step)
        except Exception as exc:
            error = type(exc).__name__
        result = dict(
            case_id=case["case_id"],
            track=case["track"],
            steps=observed,
            error_code=error,
            usage=meter.usage(meter.calls[start:]),
        )
        results.append(result)
        if on_result:
            on_result(result)
        if budget.exhausted or (provider is not None and not budget.usage_complete):
            break
    return results
