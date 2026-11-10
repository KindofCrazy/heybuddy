"""Input-only replay over persistent per-scenario production state. No gold import."""

import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path

from personal_agent.core.budget import ContextBudget
from personal_agent.core.models import AssistantMessage, ModelResponse, RunResult, TextMessage
from personal_agent.evaluation.heybuddy_bench.adapters import (
    FixedClock,
    ObservedLearning,
    memory_contents,
    seed_memory,
    seed_run,
)
from personal_agent.evaluation.heybuddy_bench.runner import ContractProvider, Meter
from personal_agent.learning.models import LearningPolicy
from personal_agent.memory.store import MemoryFS
from personal_agent.organization.models import OrganizationPolicy, Thread
from personal_agent.organization.provider import OrganizerModel
from personal_agent.organization.router import ThreadRouter
from personal_agent.proactive.models import ProactivePolicy
from personal_agent.proactive.runtime import ProactiveRuntime
from personal_agent.reflection.service import ReflectionService
from personal_agent.sessions import SessionManager
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.learning import LearningRepository
from personal_agent.storage.proactive import ProactiveRepository
from personal_agent.storage.reflection import ReflectionRepository
from personal_agent.storage.routing import RoutingRepository
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.storage.thread_history import SQLiteThreadHistory


class SequenceContractProvider(ContractProvider):
    async def generate(self, messages, tools, **options):
        data = json.loads(messages[-1].text)
        if data.get("response_schema", {}).get("title") == "ScoutResult":
            return ModelResponse(
                text=json.dumps(
                    dict(
                        changes=[],
                        followups=[
                            dict(
                                proposal_id=p["id"],
                                proposal_revision=p["revision"],
                                outcome="keep",
                                sources=[],
                            )
                            for p in data["existing"]
                            if p.get("needs_followup_review")
                        ],
                    )
                )
            )
        return await super().generate(messages, tools, **options)


class Observer:
    def __init__(self, meter):
        self.meter = meter
        self.model_candidates = None

    @property
    def calls(self):
        return self.meter.calls

    async def generate(self, messages, tools, **options):
        body = json.loads(messages[-1].text)
        if "threads" in body:
            self.model_candidates = [t["id"] for t in body["threads"]]
        return await self.meter.generate(messages, tools, **options)


def seed_threads(store, sessions, body):
    repo = RoutingRepository(store, sessions.agent.id, sessions.current.id, "cli")
    with store._transaction() as db:
        for card in body["threads"]:
            thread = Thread(**card)
            db.execute(
                "INSERT INTO topic_threads (id,agent_id,revision,dirty,last_active_at,payload) "
                "VALUES(?,?,1,0,?,?)",
                (thread.id, sessions.agent.id, card["last_active_at"], thread.model_dump_json()),
            )
            db.execute(
                "INSERT INTO thread_versions VALUES(?,1,?,'[]')",
                (thread.id, thread.model_dump_json()),
            )
    for event in body["history"]:
        seed_run(store, sessions.current.id, [event], routing=repo, thread_id=event["thread_id"])
    with store._transaction() as db:
        repo._select(db, body["active"])
    return repo


async def route_step(store, sessions, repo, step, provider):
    previous = repo.active()
    candidates = [c.id for c in repo.candidates()]
    with store.reading() as db:
        existing_ids = {
            r[0]
            for r in db.execute(
                "SELECT id FROM topic_threads WHERE agent_id=?", (sessions.agent.id,)
            )
        }
    provider.model_candidates = None
    run = store.begin_run(sessions.current.id, TextMessage(role="user", text=step["text"]))
    router = ThreadRouter(repo, model_selection=True)
    thread = await router.route(run, step["text"], provider)
    reason = router.metrics["reason"]
    action = "select"
    if router.metrics.get("ambiguous") or reason == "model_clarify":
        action = "clarify"
    elif reason in {"chat", "model_chat"}:
        action = "chat"
    elif thread.id not in existing_ids:
        action = "new"
    elif previous and thread.id == previous.id:
        action = "continue"
    store.append_message(run, AssistantMessage(text="已收到。"))
    store.finish_run(run, RunResult(status="completed", rounds=0))
    with store._transaction() as db:
        db.execute(
            "UPDATE runs SET started_at=?,ended_at=? WHERE id=?", (step["at"], step["at"], run)
        )
    history = SQLiteThreadHistory(store, sessions.current.id)
    history.thread_id = thread.id
    context = "\n".join(
        m.model_dump_json() for turn in history.page(0).turns for m in turn.messages
    )
    return dict(
        action=action,
        thread_id=thread.id,
        reason=reason,
        candidates=candidates,
        effective_candidates=list(dict.fromkeys(candidates + ([previous.id] if previous else []))),
        model_candidates=provider.model_candidates,
        context=context,
        error_code="selection_unavailable" if reason == "selection_unavailable" else None,
    )


def learning_service(store, sessions, root, clock, provider, files):
    memory = MemoryFS.for_agent(root / "memory", sessions.agent.id)
    seed_memory(memory, files)
    service = ObservedLearning(
        LearningRepository(EpisodeRepository(store, sessions.agent.id)),
        ReflectionService(
            ReflectionRepository(store, sessions.agent.id),
            memory,
            provider,
            ContextBudget(context_window=131072),
        ),
        LearningPolicy(),
        clock=clock,
    )
    return memory, service


async def proactive_step(runtime, store, sessions, step, run_ids):
    if step.get("text"):
        events = [dict(text=step["text"], timestamp=step["at"])]
        if step.get("assistant"):
            events.append(dict(text=step["assistant"], timestamp=step["at"], speaker="assistant"))
        run_ids[seed_run(store, sessions.current.id, events)] = step["id"]
    stages = []
    # Drain only new source evidence, not repeat Attention until a preferred answer appears.
    for _ in range(16):
        if not runtime.repository.pending():
            break
        result = await runtime.run()
        stages.append(result)
        if result["outcome"] not in {"completed", "no_proactive_work"}:
            break
    if runtime.repository.pending():
        return dict(error_code="source_drain_incomplete")
    decisions_before = runtime.repository.listing("decisions")["items"]
    if step["attention"]:
        stages.append(await runtime.run())
    proposals = runtime.repository.listing("proposals")["items"]
    for proposal in proposals:
        refs = proposal["sources"] + proposal["covered_sources"]
        proposal["event_ids"] = sorted(
            {run_ids[r["run_id"]] for r in refs if r["run_id"] in run_ids}
        )
    decisions = runtime.repository.listing("decisions")["items"]
    decision = (
        decisions[0]["payload"].get("decision")
        if decisions and (not decisions_before or decisions[0]["id"] != decisions_before[0]["id"])
        else None
    )
    selected = [p for p in proposals if decision and p["id"] in decision["proposal_ids"]]
    error = next(
        (
            s.get("error_code", s["outcome"])
            for s in stages
            if s["outcome"] in {"failed", "blocked", "budget_exhausted"}
        ),
        None,
    )
    return dict(
        proposals=proposals,
        action=decision["action"] if decision else "none",
        selected_text="\n".join(p["title"] + " " + p["context"] for p in selected),
        gate=runtime.gate(),
        stages=stages,
        error_code=error,
    )


async def replay(case, root, provider, output_steps):
    """Persist only within this scenario, disclose one chronological step at a time."""
    body, track = case["input"], case["track"]
    clock = FixedClock(body["steps"][0]["at"])
    with SQLiteStore(root / "sequence.sqlite3") as store:
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
        run_ids = {}
        for step in body["steps"]:
            clock.at = datetime.fromisoformat(step["at"])
            if track == "routing":
                observed = await route_step(store, sessions, repo, step, provider)
            elif track == "memory":
                events = [dict(text=step["text"], timestamp=step["at"])]
                if step.get("assistant"):
                    events.append(
                        dict(text=step["assistant"], timestamp=step["at"], speaker="assistant")
                    )
                run = seed_run(store, sessions.current.id, events)
                learner.observed_decisions = []
                outcome = (
                    await learner.learn(run_ids=[run])
                    if step["learn"]
                    else dict(outcome="not_requested")
                )
                observed = dict(
                    files=memory_contents(memory),
                    outcome=outcome,
                    decisions=getattr(learner, "observed_decisions", []),
                    error_code=outcome.get("error_code")
                    or (
                        outcome["outcome"]
                        if outcome["outcome"] not in {"completed", "not_requested"}
                        else None
                    ),
                )
            else:
                observed = await proactive_step(runtime, store, sessions, step, run_ids)
            output_steps.append(observed)
            if observed.get("error_code"):
                break


async def run_sequences(cases, *, temp_parent: Path, budget, provider=None, on_result=None):
    temp_parent.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    meter = Meter(provider or SequenceContractProvider(), budget, offline=provider is None)
    observer = Observer(meter)
    results = []
    for case in cases:
        steps, error, start = [], None, len(meter.calls)
        try:
            async with asyncio.timeout(240):
                with tempfile.TemporaryDirectory(prefix="sequence-", dir=temp_parent) as name:
                    await replay(case, Path(name), observer, steps)
        except Exception as exc:
            error = type(exc).__name__  # No SDK/Pydantic exception bodies or credentials.
        result = dict(
            case_id=case["case_id"],
            track=case["track"],
            steps=steps,
            error_code=error,
            usage=meter.usage(meter.calls[start:]),
        )
        results.append(result)
        if on_result:
            on_result(result)
        if budget.exhausted or (provider is not None and not budget.usage_complete):
            break
    return results
