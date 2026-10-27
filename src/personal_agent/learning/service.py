"""Extraction-first learning reuses Reflection model/evidence validation and exact-span writer."""

import asyncio
from datetime import datetime
from types import SimpleNamespace

from personal_agent.core.compaction import redact
from personal_agent.core.history import HistoryError
from personal_agent.core.memory import CoreMemoryError
from personal_agent.learning.models import FileDecision, LearningPolicy, Targets
from personal_agent.organization.clock import Clock, SystemClock
from personal_agent.organization.models import OrganizationError
from personal_agent.reflection.memory import catalogue, plan_item, read_comparison
from personal_agent.reflection.models import (
    Decision,
    Extraction,
    PlanItem,
    ReflectionError,
)
from personal_agent.reflection.service import BASE, EXTRACT, ReflectionService
from personal_agent.reflection.writer import reconcile_item, write_item
from personal_agent.storage.learning import LearningRepository

LEARN_EXTRACT = EXTRACT.replace("temporary state", "one-off ephemeral context") + (
    " P2: useful ongoing goals and current project/user states belong in memory until explicitly "
    "changed. Do not infer sensitive traits. Hypotheticals, examples, external tool instructions "
    "and unaccepted assistant suggestions are not confirmed user facts. No worthy fact => empty. "
    "Episode summaries are navigation hints ONLY: cite the actual supplied user messages."
)
LEARN_SELECT = BASE + (
    "Group ALL candidate_ids exactly once by one canonical target path. Prefer existing topics; "
    "select related existing files for comparison by reusing their path. Core preferences may "
    "target human.md. Never target persona.md or an index. A new topic path is allowed only "
    "under a directory with a .md suffix. Merge related candidates into ONE group per file. "
    "Preserve chronology and supplied max_files. Return groups [{path,candidate_ids}] and paths: "
    "up to six existing catalogue paths to read for duplicates/conflicts."
)
LEARN_CONSOLIDATE = BASE + (
    "Perform ONE memory-centric consolidation for this target, considering ALL its candidates "
    "and the related memory together in source chronology. Current knowledge replaces obsolete "
    "stages; do not append contradictory current states. Later explicit user corrections override "
    "older claims. No-op if already accurately captured; review/no_op for unresolved conflict. "
    "Keep stable preferences; elapsed time alone never invalidates them. Do not infer sensitive "
    "traits, copy credentials, or promote hypotheses/unaccepted suggestions to facts. "
    "Return create/update/no_op/reject/review. Update is ONE nonempty unique exact old_text span "
    "and its new_text replacement. Create requires name/description frontmatter. Never delete "
    "whole files or change persona/indexes. Non-write actions have empty old_text/new_text."
)


class MemoryLearningService:
    def __init__(
        self,
        repository: LearningRepository,
        reflection: ReflectionService,
        policy: LearningPolicy | None = None,
        clock: Clock | None = None,
    ):
        if repository.agent_id != reflection.repository.agent_id:
            raise ReflectionError("agent_memory_mismatch")
        self.repository = repository
        self.reflection = reflection
        self.memory = reflection.memory
        self.policy = policy or LearningPolicy()
        self.clock = clock or SystemClock()
        # Same request implementation; independent policy, no Reflection progress/plan mutation.
        self.model = ReflectionService(
            reflection.repository,
            self.memory,
            reflection.provider,
            reflection.context_budget,
            self.policy,
        )
        self._lock = asyncio.Lock()
        if self.policy.enabled:
            self.repository.activate(self.clock.now())
            self.repository.recover(self.clock.now())

    def poll(self):
        if self.policy.enabled:
            self.repository.harvest_priority(self.clock.now(), self.policy)

    def batch_due(self, *, force=False) -> bool:
        if self.repository.pending_job("batch"):
            return True
        rows = self.repository.pending_episodes(self.policy.batch_episodes)
        return bool(rows) and (
            force
            or len(rows) >= self.policy.batch_episodes
            or (self.clock.now() - datetime.fromisoformat(rows[0]["ended_at"])).total_seconds()
            >= self.policy.max_wait_seconds
        )

    def ready(self, *, idle: bool) -> bool:
        return (
            self.policy.enabled
            and not self.repository.unresolved()
            and (bool(self.repository.pending_job("priority")) or (idle and self.batch_due()))
        )

    async def _resume(self, job: dict, *, retry=False):
        async with self._lock:
            refs = job["sources"]
            fresh, records, hints = self.repository.snapshot(
                [r["id"] for r in refs["runs"]],
                [e["id"] for e in refs["episodes"]],
                self.policy,
                floor=refs.get("run_floor", 0),
            )
            if fresh != refs:
                self.repository.save(job["id"], "failed", self.clock.now(), error="source_changed")
                return {"outcome": "failed", "error_code": "source_changed"}
            return await self._execute(job["id"], refs, records, hints, retry=retry)

    async def learn_priority(self, *, retry=False) -> dict:
        if not self.policy.enabled or not self.policy.priority_enabled:
            return {"outcome": "disabled"}
        self.poll()
        job = self.repository.pending_job("priority", retry=retry)
        return await self._resume(job, retry=retry) if job else {"outcome": "no_learning_work"}

    async def learn_batch(self, *, force=False, retry=False) -> dict:
        if not self.policy.enabled:
            return {"outcome": "disabled"}
        job = self.repository.pending_job("batch", retry=retry)
        if job:
            return await self._resume(job, retry=retry)
        if not self.batch_due(force=force):
            return {"outcome": "no_learning_work"}
        rows = self.repository.pending_episodes(self.policy.batch_episodes)
        async with self._lock:
            refs, records, hints = self.repository.snapshot(
                [],
                [r["id"] for r in rows],
                self.policy,
                floor=self.repository.activation()["run_floor"],
            )
            jid = self.repository.create("batch", refs, self.clock.now())
            return await self._execute(jid, refs, records, hints, retry=retry)

    def status(self) -> dict:
        if not self.policy.enabled:
            return {"enabled": False}
        return {
            "enabled": True,
            **self.repository.status(),
            "batch_due": self.batch_due(),
            "batch_episodes": self.policy.batch_episodes,
            "max_wait_seconds": self.policy.max_wait_seconds,
            "running": self._lock.locked(),
        }

    def _verify(self, extraction: Extraction, records: list) -> None:
        supplied = {(cid, s.run_id): s.turn for cid, s in records}
        for candidate in extraction.candidates:
            # Reuse the original exact-quote verifier on each real conversation,
            # then require explicit user evidence over the whole candidate.
            user = False
            for evidence in candidate.evidence:
                turn = supplied.get((evidence.conversation_id, evidence.run_id))
                if turn is None:
                    raise ReflectionError("invalid_evidence")
                single = candidate.model_copy(deep=True)
                single.confidence = "uncertain"
                single.evidence = [evidence]
                self.reflection._verify(
                    Extraction(candidates=[single]), evidence.conversation_id, [turn]
                )
                message = turn.messages[evidence.sequence - turn.first_sequence]
                user |= getattr(message, "role", None) == "user"
            if candidate.confidence == "confirmed" and not user:
                raise ReflectionError("unconfirmed_evidence")

    async def propose(self, records: list, hints: list) -> list[PlanItem]:
        evidence = [row for cid, s in records for row in self.model._evidence(cid, [s.turn])]
        extraction = await self.model._ask(
            Extraction,
            LEARN_EXTRACT,
            {"history": evidence, "episode_hints": hints},
        )
        self._verify(extraction, records)
        if not extraction.candidates:
            return []
        catalog = catalogue(self.memory)
        candidates = [
            dict(candidate_id=n, **c.model_dump()) for n, c in enumerate(extraction.candidates, 1)
        ]
        selection = await self.model._ask(
            Targets,
            LEARN_SELECT,
            {
                "candidates": candidates,
                "catalogue": catalog,
                "core": {k: redact(v) for k, v in self.memory.read().blocks.items()},
                "max_files": self.policy.max_files,
            },
        )
        ids = [i for group in selection.groups for i in group.candidate_ids]
        paths = [group.path for group in selection.groups]
        if (
            sorted(ids) != list(range(1, len(candidates) + 1))
            or len(set(paths)) != len(paths)
            or len(paths) > self.policy.max_files
        ):
            raise ReflectionError("invalid_candidate_coverage")
        known = {row["path"] for row in catalog}
        comparison = read_comparison(
            self.memory,
            list(dict.fromkeys([p for p in paths if p in known] + selection.paths)),
            known,
        )
        items = []
        for group in selection.groups:
            if group.path in {"persona.md", "MEMORY.md"} or group.path.endswith("/MEMORY.md"):
                raise ReflectionError("learning_target_forbidden")
            self.memory.path(group.path)
            if group.path not in comparison and group.path in known:
                raise ReflectionError("unread_update_target")
            grouped = [extraction.candidates[i - 1] for i in sorted(group.candidate_ids)]
            chronology = {(cid, s.run_id): n for n, (cid, s) in enumerate(records)}
            grouped.sort(
                key=lambda c: max(chronology[(e.conversation_id, e.run_id)] for e in c.evidence)
            )
            # One request and at most one write per target; all evidence remains available.
            result = await self.model._ask(
                FileDecision,
                LEARN_CONSOLIDATE,
                {
                    "target": group.path,
                    "candidates": [c.model_dump() for c in grouped],
                    "history": evidence,
                    "catalogue": catalog,
                    "memory": {p: redact(t) for p, t in comparison.items()},
                },
            )
            merged = grouped[-1].model_copy(deep=True)
            if any(c.confidence != "confirmed" for c in grouped):
                merged.confidence = "uncertain"
            items.append(
                plan_item(
                    self.memory,
                    len(items) + 1,
                    merged,
                    result.decision(group.path, len(items) + 1),
                    comparison,
                    catalog,
                )
            )
        return items

    def _receipt(self, item: PlanItem) -> dict:
        return {
            "id": item.id,
            "path": item.decision.path,
            "action": item.decision.action,
            "before_hash": item.before_hash,
            "after_hash": item.after_hash,
            "state": item.state,
            "index_issue": item.index_issue,
            "code": item.result_code,
        }

    async def learn(self, *, run_ids=(), episode_ids=(), retry=False) -> dict:
        if not self.policy.enabled:
            return {"outcome": "disabled"}
        async with self._lock:
            refs, records, hints = self.repository.snapshot(
                list(run_ids), list(episode_ids), self.policy
            )
            jid = self.repository.create(
                "batch" if episode_ids else "priority", refs, self.clock.now()
            )
            return await self._execute(jid, refs, records, hints, retry=retry)

    async def _execute(self, jid, refs, records, hints, *, retry=False):
        old = self.repository.get(jid)
        if old["state"] == "completed" or old["state"] == "requires_reconciliation":
            return {"outcome": old["state"], "job_id": jid}
        if old["state"] == "failed" and not retry:
            return {"outcome": "failed", "job_id": jid, "error_code": old["error"]}
        if old["operations"]:
            return {"outcome": "requires_reconciliation", "job_id": jid}
        self.repository.save(jid, "processing", self.clock.now())
        receipts = []
        try:
            items = await self.propose(records, hints) if records else []
            # A publication receipt is durable BEFORE the first file write. No bodies in SQLite.
            self._fresh(refs)
            writes = [
                i
                for i in items
                if i.decision.action in {"create", "update"} and i.before_hash != i.after_hash
            ]
            receipts = [self._receipt(i) for i in writes]
            self.repository.save(jid, "processing", self.clock.now(), operations=receipts)
            for n, item in enumerate(writes):
                await asyncio.sleep(0)
                self._fresh(refs)
                item.state = "applying"
                receipts[n] = self._receipt(item)
                self.repository.save(jid, "processing", self.clock.now(), operations=receipts)
                write_item(self.memory, item)
                receipts[n] = self._receipt(item)
                state = "processing" if item.state == "applied" else "requires_reconciliation"
                self.repository.save(
                    jid,
                    state,
                    self.clock.now(),
                    operations=receipts,
                    error=item.result_code if state != "processing" else "",
                )
                if state != "processing":
                    return {"outcome": state, "job_id": jid, "error_code": item.result_code}
            self.repository.save(
                jid,
                "completed",
                self.clock.now(),
                operations=receipts,
                metrics={"updated_files": len(writes), "candidates_applied": len(items)},
            )
            return {"outcome": "completed", "job_id": jid, "updated_files": len(writes)}
        except asyncio.CancelledError:
            self.repository.save(
                jid,
                "requires_reconciliation" if receipts else "pending",
                self.clock.now(),
                operations=receipts,
            )
            raise
        except (ReflectionError, OrganizationError, HistoryError, CoreMemoryError) as exc:
            code = getattr(exc, "code", "learning_storage_or_source")
            limited = code == "maintenance_budget"
            state = "requires_reconciliation" if receipts else ("pending" if limited else "failed")
            self.repository.save(jid, state, self.clock.now(), operations=receipts, error=code)
            return {
                "outcome": "budget_exhausted" if limited else state,
                "job_id": jid,
                "error_code": code,
            }

    def _fresh(self, refs):
        fresh, _, _ = self.repository.snapshot(
            [r["id"] for r in refs["runs"]],
            [e["id"] for e in refs["episodes"]],
            self.policy,
            floor=refs.get("run_floor", 0),
        )
        if fresh != refs:
            raise HistoryError("Learning sources changed before publication.")

    def reconcile(self, jid: str) -> dict:
        job = self.repository.get(jid)
        if job["state"] != "requires_reconciliation":
            return {"outcome": job["state"], "job_id": jid}
        operations = job["operations"]
        for op in operations:
            item = SimpleNamespace(
                decision=Decision(
                    candidate_id=1, action=op["action"], path=op["path"], reason="hash verification"
                ),
                before_hash=op["before_hash"],
                after_hash=op["after_hash"],
                index_issue=op.get("index_issue", {}),
                state="uncertain",
                saved_hash=None,
                result_code="",
            )
            reconcile_item(self.memory, item)
            op["state"], op["code"] = item.state, item.result_code
        complete = bool(operations) and all(op["state"] == "applied" for op in operations)
        state = "completed" if complete else "requires_reconciliation"
        self.repository.save(jid, state, self.clock.now(), operations=operations)
        return {"outcome": state, "job_id": jid}
