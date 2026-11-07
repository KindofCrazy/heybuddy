"""Real production calls over fresh snapshot fixtures; never assembles a channel/Sender."""

import json
from datetime import datetime, timedelta
from pathlib import Path

from personal_agent.core.budget import ContextBudget
from personal_agent.core.models import AssistantMessage, RunResult, TextMessage
from personal_agent.learning.models import LearningPolicy
from personal_agent.learning.service import MemoryLearningService
from personal_agent.memory import index
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
from personal_agent.storage.proactive import ProactiveRepository, stamp
from personal_agent.storage.reflection import ReflectionRepository
from personal_agent.storage.routing import RoutingRepository
from personal_agent.storage.sqlite import SQLiteStore


class FixedClock:
    def __init__(self, now: str):
        self.at = datetime.fromisoformat(now)

    def now(self):
        return self.at


class ObservedLearning(MemoryLearningService):
    """Observe the actual host-validated plans without changing the production pipeline."""

    async def propose(self, records, hints):
        items = await super().propose(records, hints)
        self.observed_decisions = [item.decision.model_dump() for item in items]
        return items


def seed_run(store, conversation, events, *, routing=None, thread_id=None):
    """Preserve roles and timestamps. Assistant input cannot become a user quotation."""
    first = events[0]
    if first.get("speaker", "user") != "user":
        raise ValueError("Snapshot Run must start with a user")
    run = store.begin_run(conversation, TextMessage(role="user", text=first["text"]))
    if routing and thread_id:
        routing.bind(run, thread_id, "snapshot")
    for event in events[1:]:
        message = (
            AssistantMessage(text=event["text"])
            if event.get("speaker") == "assistant"
            else TextMessage(role="user", text=event["text"])
        )
        store.append_message(run, message)
    if events[-1].get("speaker", "user") != "assistant":
        store.append_message(run, AssistantMessage(text="已收到。"))
    store.finish_run(run, RunResult(status="completed", rounds=0))
    with store._transaction() as db:
        db.execute(
            "UPDATE runs SET started_at=?,ended_at=? WHERE id=?",
            (first["timestamp"], events[-1]["timestamp"], run),
        )
    return run


async def routing(body, store, sessions, provider, *, model_selection=True):
    repo = RoutingRepository(store, sessions.agent.id, sessions.current.id, "cli")
    for card in body["thread_candidates"]:
        thread = Thread(
            id=card["id"],
            title=card["title"],
            current_state=card["current_state"],
            last_active_at=card["last_active_at"],
        )
        with store._transaction() as db:
            db.execute(
                "INSERT INTO topic_threads "
                "(id,agent_id,revision,dirty,last_active_at,payload) VALUES(?,?,1,0,?,?)",
                (thread.id, sessions.agent.id, card["last_active_at"], thread.model_dump_json()),
            )
            db.execute(
                "INSERT INTO thread_versions VALUES(?,1,?,'[]')",
                (thread.id, thread.model_dump_json()),
            )
    history = []
    for episode in body["history"]:
        history.append(
            {
                "thread_id": episode["thread_id"],
                "timestamp": episode["timestamp"],
                "text": "\n".join(episode["messages"]),
            }
        )
    history.extend(body["recent_context"])
    for event in sorted(history, key=lambda e: e["timestamp"]):
        seed_run(store, sessions.current.id, [event], routing=repo, thread_id=event["thread_id"])
    # Snapshot selection is state, not a fresh /thread select command (which bypasses routing).
    with store._transaction() as db:
        db.execute("DELETE FROM active_threads")
        if body["active_thread_id"]:
            repo._select(db, body["active_thread_id"])
    previous = body["active_thread_id"]
    run = store.begin_run(
        sessions.current.id, TextMessage(role="user", text=body["current_message"])
    )
    router = ThreadRouter(repo, model_selection=model_selection)
    result = await router.route(run, body["current_message"], provider)
    reason = router.metrics["reason"]
    if reason == "selection_unavailable":
        action = "__error__"
    elif router.metrics.get("ambiguous") or reason == "model_clarify":
        action = "clarify"
    elif reason in {"chat", "model_chat"}:
        action = "chat"
    elif result.id not in {c["id"] for c in body["thread_candidates"]}:
        action = "new"
    else:
        action = "continue" if result.id == previous else "select"
    return {
        "action": action,
        "thread_id": result.id if action in {"continue", "select"} else None,
        "bound_thread_id": result.id,
        "routing_reason": reason,
    }


def seed_memory(memory, files):
    memory.initialize()
    for path, text in files.items():
        if path in {"human.md", "persona.md"}:
            memory.update(path, text, expected_hash=memory.read().hashes[path])
        else:
            # Old seed topics omitted mandatory metadata; add neutral navigation metadata.
            if not text.startswith("---\n"):
                text = (
                    "---\nname: "
                    + Path(path).stem
                    + "\ndescription: Snapshot notes\n---\n\n"
                    + text
                )
            index.create(memory, path, text)


def memory_contents(memory):
    return {
        p.relative_to(memory.root).as_posix(): p.read_text(encoding="utf-8")
        for p in memory.root.rglob("*.md")
        if p.name != "MEMORY.md"
    }


async def learning(body, store, sessions, provider, root):
    call_start = len(provider.calls)
    memory = MemoryFS.for_agent(root / "memory", sessions.agent.id)
    seed_memory(memory, body["memory_files"])
    before = memory_contents(memory)
    clock = FixedClock(body["now"])
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
    events = body["episode"]["events"]
    # Each source user event is a completed Run, retaining adjacent assistant text if supplied.
    groups = []
    for event in events:
        if event.get("speaker", "user") == "user":
            groups.append([event])
        elif groups:
            groups[-1].append(event)
        else:
            raise ValueError("Assistant-only source")
    runs = [seed_run(store, sessions.current.id, group) for group in groups]
    result = await service.learn(run_ids=runs)
    job = service.repository.get(result["job_id"])
    after = memory_contents(memory)
    changes = [
        {
            "path": path,
            "before": before.get(path),
            "after": text,
            "action": "update" if path in before else "create",
        }
        for path, text in sorted(after.items())
        if text != before.get(path)
    ]
    model_decisions = [
        call["parsed"]
        for call in provider.calls[call_start:]
        if call.get("schema") in {"FileDecision", "ExistingFileDecision"} and call.get("parsed")
    ]
    decisions = getattr(service, "observed_decisions", [])
    action = changes[0]["action"] if changes else (decisions[0]["action"] if decisions else "no_op")
    if not changes and action in {"create", "update"}:
        action = "no_op"
    if result["outcome"] != "completed":
        action = "__error__"
    return {
        "action": action,
        "target_path": changes[0]["path"] if changes else None,
        "changes": changes,
        "decisions": decisions,
        "model_decisions": model_decisions,
        "outcome": result["outcome"],
        "error_code": result.get("error_code"),
        "receipts": job["operations"],
    }


async def proactive(body, store, sessions, provider):
    """Decision snapshot: candidates are supplied, then real gate/Attention/CAS/audit run.

    Opportunity extraction is outside this track's decision-point input. No Scout outputs or
    ground truth are manufactured. The standard runtime is called with no pending source.
    """
    now = datetime.fromisoformat(body["now"])
    evidence = body["recent_user_evidence"]
    refs = {}
    for event in evidence:
        run = seed_run(store, sessions.current.id, [event])
        with store.reading() as db:
            sequence = db.execute(
                "SELECT sequence FROM messages WHERE run_id=?", (run,)
            ).fetchone()[0]
        refs[event["id"]] = dict(
            conversation_id=sessions.current.id,
            run_id=run,
            sequence=sequence,
            quote=event["text"][:512],
        )
    policy = ProactivePolicy(compose_enabled=False)
    repo = ProactiveRepository(store, sessions.agent.id)
    runtime = ProactiveRuntime(
        repo,
        OrganizerModel(
            provider,
            ContextBudget(context_window=131072),
            OrganizationPolicy(input_tokens=16384, output_tokens=4096),
        ),
        policy,
        clock=FixedClock(body["now"]),
    )
    for candidate in body["candidates"]:
        selected = [e for e in evidence if e["id"] in candidate["source_evidence_ids"]]
        last = candidate.get("last_notified_at")
        unchanged_receipt = last and all(
            datetime.fromisoformat(e["timestamp"]) <= datetime.fromisoformat(last) for e in selected
        )
        with store._transaction() as db:
            db.execute(
                "INSERT INTO proactive_proposals "
                "(id,agent_id,topic_key,title,context,sources,source_position,status,revision,"
                "next_review_at,expires_at,last_action,updated_at,followup_state) "
                "VALUES(?,?,?,?,?,?,1,?,1,?,?,?,?,?)",
                (
                    candidate["id"],
                    sessions.agent.id,
                    candidate["topic_key"],
                    candidate["title"],
                    candidate["context"],
                    json.dumps([refs[e["id"]] for e in selected]),
                    candidate["status"],
                    None if unchanged_receipt else stamp(now),
                    stamp(
                        max(datetime.fromisoformat(e["timestamp"]) for e in selected)
                        + timedelta(seconds=policy.lifetime_seconds)
                    ),
                    "notify" if unchanged_receipt else "",
                    stamp(now),
                    candidate.get("followup_state", "pending"),
                ),
            )
    outcome = await runtime.run()
    audits = repo.listing("decisions")["items"]
    decisions = [a for a in audits if "decision" in a["payload"]]
    if decisions:
        prediction = decisions[0]["payload"]["decision"]
        persisted = repo.listing("proposals")["items"]
        prediction["effective_next_review_at"] = next(
            (p["next_review_at"] for p in persisted if p["id"] in prediction["proposal_ids"]), None
        )
    else:
        prediction = dict(action="silent", proposal_ids=[], next_review_at=None)
    if outcome["outcome"] in {"failed", "stale", "blocked", "budget_exhausted"}:
        prediction["action"] = "__error__"
    return {**prediction, "runtime": outcome, "mode": "shadow", "messages_sent": 0}


async def evaluate(track, body, provider, root, *, model_selection=True):
    # root is always a newly allocated TemporaryDirectory, never a configurable user DB.
    with SQLiteStore(root / "snapshot.sqlite3") as store:
        store.initialize()
        sessions = SessionManager(store)
        if track == "thread_routing":
            return await routing(body, store, sessions, provider, model_selection=model_selection)
        if track == "memory_learning":
            return await learning(body, store, sessions, provider, root)
        return await proactive(body, store, sessions, provider)
