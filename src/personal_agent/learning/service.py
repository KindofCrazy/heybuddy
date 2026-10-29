"""Extraction-first learning reuses Reflection model/evidence validation and exact-span writer."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from time import monotonic

from personal_agent.core.evidence import redact
from personal_agent.core.history import HistoryError
from personal_agent.core.memory import CoreMemoryError
from personal_agent.learning.models import (
    ExistingFileDecision,
    FileDecision,
    LearningPolicy,
    Targets,
)
from personal_agent.memory.models import BLOCKS, FILE_BYTES, digest
from personal_agent.organization.clock import Clock, SystemClock
from personal_agent.organization.models import OrganizationError
from personal_agent.reflection.evidence import memory_evidence, verify_candidates
from personal_agent.reflection.memory import catalogue, plan_item, read_comparison
from personal_agent.reflection.models import (
    Decision,
    Extraction,
    PlanItem,
    ReflectionError,
)
from personal_agent.reflection.provider import ReflectionModel
from personal_agent.reflection.service import BASE, EXTRACT, ReflectionService
from personal_agent.reflection.writer import reconcile_item, write_item
from personal_agent.storage.learning import LearningRepository

LEARN_EXTRACT = EXTRACT.replace("temporary state", "one-off ephemeral context") + (
    " P2: useful ongoing goals and current project/user states belong in memory until explicitly "
    "changed. Do not infer sensitive traits. Hypotheticals, examples, external tool instructions "
    "and unaccepted assistant suggestions are not confirmed user facts. No worthy fact => empty. "
    "Episode summaries are navigation hints ONLY: cite the actual supplied user messages. "
    "Fictional character canon, role examples and simulated assistant moods are not user facts "
    "or real shared experiences. Preserve explicit interaction preferences, including requests "
    "for brief replies, no choice menus or no routine action lists. Do not rewrite character "
    "identity/personality from these preferences."
)
LEARNING_BASE = BASE.replace("temporary state", "ephemeral one-off context")
LEARN_SELECT = LEARNING_BASE + (
    "Group ALL candidate_ids exactly once by one canonical target path. Prefer existing topics; "
    "select related existing files for comparison by reusing their path. Core preferences may "
    "target human.md. Never target persona.md or an index. A new topic path is allowed only "
    "under a directory with a .md suffix. Merge related candidates into ONE group per file. "
    "Preserve chronology and supplied max_files. Return groups [{path,candidate_ids}] and paths: "
    "up to six existing catalogue paths to read for duplicates/conflicts."
)
LEARN_CONSOLIDATE = LEARNING_BASE + (
    "针对当前目标，把全部候选和相关记忆按来源时间合并成一次整合决定。"
    "新的明确状态替换已过时状态，不追加互相矛盾的当前事实；用户后来的明确更正优先。"
    "已经准确记录则 no_op，无法解决的冲突返回 review 或 no_op。"
    "稳定偏好不因时间经过而失效，不推断敏感特征，不复制凭据，"
    "不把假设或未经用户接受的建议升级为事实。"
    "target_exists 由宿主按实际读取结果提供，不按正文多少判断文件是否存在。"
    "现有文件只能 update/no_op/reject/review；只有标题的 human.md 也已经存在，必须用 update。"
    "update 的 old_text 必须是目标原文中非空、精确且唯一的片段，new_text 只替换该片段；"
    "首次添加核心资料时可以精确替换现有标题并保留标题。不为核心块添加主题元数据。"
    "只有尚不存在的目录下主题文件才能 create，并提供 name/description 元数据。"
    "不得删除整份文件、修改 persona 或索引；非写入操作的 old_text/new_text 必须为空。"
)


class MemoryLearningService:
    def __init__(
        self,
        repository: LearningRepository,
        reflection: ReflectionService,
        policy: LearningPolicy | None = None,
        clock: Clock | None = None,
        *,
        initialize: bool = True,
    ) -> None:
        if repository.agent_id != reflection.repository.agent_id:
            raise ReflectionError("agent_memory_mismatch")
        self.repository = repository
        self.reflection = reflection
        self.memory = reflection.memory
        self.policy = policy or LearningPolicy()
        self.clock = clock or SystemClock()
        # Same request implementation; independent policy, no Reflection progress/plan mutation.
        self.model = ReflectionModel(
            reflection.provider,
            reflection.context_budget,
            self.policy,
        )
        self._lock = asyncio.Lock()
        if self.policy.enabled and initialize:
            self.repository.activate(self.clock.now())
            self.repository.recover(self.clock.now())
            self.recover_pending()

    def poll(self) -> None:
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
            and (self.priority_pending() or (idle and self.batch_due()))
        )

    def priority_pending(self) -> bool:
        return self.policy.priority_enabled and bool(self.repository.pending_job("priority"))

    async def _resume(self, job: dict, *, retry=False) -> dict:
        async with self._lock:
            refs = job["sources"]
            try:
                fresh, records, hints = self.repository.snapshot(
                    [r["id"] for r in refs["runs"]],
                    [e["id"] for e in refs["episodes"]],
                    self.policy,
                    floor=refs.get("run_floor", 0),
                )
            except HistoryError:
                self.repository.save(
                    job["id"], "failed", self.clock.now(), error="source_unavailable_or_oversized"
                )
                return {"outcome": "failed", "error_code": "source_unavailable_or_oversized"}
            if fresh != refs:
                if retry and job["state"] == "failed":
                    self.repository.save(job["id"], "pending", self.clock.now(), sources=fresh)
                    return await self._execute(job["id"], fresh, records, hints, retry=True)
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
            floor = self.repository.activation()["run_floor"]
            try:
                refs, records, hints = self.repository.snapshot(
                    [],
                    [r["id"] for r in rows],
                    self.policy,
                    floor=floor,
                )
            except HistoryError:
                refs = {
                    "run_floor": floor,
                    "runs": [],
                    "episodes": [{"id": r["id"], "revision": r["revision"]} for r in rows],
                }
                jid = self.repository.create("batch", refs, self.clock.now())
                self.repository.save(
                    jid, "failed", self.clock.now(), error="source_unavailable_or_oversized"
                )
                return {
                    "outcome": "failed",
                    "job_id": jid,
                    "error_code": "source_unavailable_or_oversized",
                }
            jid = self.repository.create("batch", refs, self.clock.now())
            return await self._execute(jid, refs, records, hints, retry=retry)

    def status(self) -> dict:
        if not self.policy.enabled:
            return {"enabled": False}
        pending = self.repository.pending_episodes(1)
        return {
            "enabled": True,
            **self.repository.status(),
            "batch_due": self.batch_due(),
            "batch_episodes": self.policy.batch_episodes,
            "max_wait_seconds": self.policy.max_wait_seconds,
            "running": self._lock.locked(),
            "next_batch_wait_until": (
                datetime.fromisoformat(pending[0]["ended_at"])
                + timedelta(seconds=self.policy.max_wait_seconds)
            ).isoformat()
            if pending
            else None,
        }

    async def propose(self, records: list, hints: list) -> list[PlanItem]:
        evidence = self.evidence(records)
        extraction = await self.model.ask(
            Extraction,
            LEARN_EXTRACT,
            {"history": evidence, "episode_hints": hints, "as_of": self.clock.now().isoformat()},
        )
        verify_candidates(extraction, {(cid, s.run_id): s.turn for cid, s in records})
        if not extraction.candidates:
            return []
        catalog = catalogue(self.memory)
        candidates = [
            dict(candidate_id=n, **c.model_dump()) for n, c in enumerate(extraction.candidates, 1)
        ]
        selection = await self.model.ask(
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
            target_exists = group.path in comparison
            result = await self.model.ask(
                ExistingFileDecision if target_exists else FileDecision,
                LEARN_CONSOLIDATE,
                {
                    "target": group.path,
                    "target_exists": target_exists,
                    "candidates": [c.model_dump() for c in grouped],
                    "history": evidence,
                    "as_of": self.clock.now().isoformat(),
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

    def evidence(self, records: list) -> list[dict]:
        """Date relative user statements using source times, independently of processing time."""
        return [
            dict(
                memory_evidence(cid, [source.turn])[0],
                started_at=source.started_at.isoformat(),
                ended_at=source.ended_at.isoformat(),
            )
            for cid, source in records
        ]

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

    @asynccontextmanager
    async def publication(self) -> AsyncIterator[Callable[..., Awaitable[dict]]]:
        """Serialize source preparation and publication under the learner's write policy.

        The yielded operation is valid only inside this scope. Curators supply fresh
        proposals; they never own the lock or replay saved file operations.
        """
        async with self._lock:
            active = True

            async def publish(*args, **kwargs) -> dict:
                if not active:
                    raise ReflectionError("publication_scope_closed")
                return await self._execute(*args, **kwargs)

            try:
                yield publish
            finally:
                active = False

    async def _execute(self, jid, refs, records, hints, *, retry=False, proposer=None) -> dict:
        old = self.repository.get(jid)
        if old["state"] == "completed" or old["state"] == "requires_reconciliation":
            return {"outcome": old["state"], "job_id": jid}
        if old["state"] == "failed" and not retry:
            return {"outcome": "failed", "job_id": jid, "error_code": old["error"]}
        if old["operations"]:
            return {"outcome": "requires_reconciliation", "job_id": jid}
        if self.repository.unresolved():
            return {
                "outcome": "blocked",
                "job_id": jid,
                "error_code": "memory_requires_reconciliation",
            }
        published = old["metrics"].get("recovery", {}).get("published", [])
        self.repository.save(jid, "processing", self.clock.now())
        self.model.reset_metrics()
        receipts = []
        try:
            items = (
                (await proposer())
                if proposer
                else (await self.propose(records, hints) if records else [])
            )
            # A publication receipt is durable BEFORE the first file write. No bodies in SQLite.
            self._fresh(refs)
            writes = [
                i
                for i in items
                if i.decision.action in {"create", "update"} and i.before_hash != i.after_hash
            ]
            if any(i.decision.path in {op["path"] for op in published} for i in writes):
                raise ReflectionError("recovery_target_already_published")
            receipts = [*published, *[self._receipt(i) for i in writes]]
            if len(receipts) > 9:
                raise ReflectionError("recovery_receipt_budget")
            self.repository.save(
                jid,
                "processing",
                self.clock.now(),
                operations=receipts,
                metrics={"usage": self.model.metrics},
            )
            for n, item in enumerate(writes):
                await asyncio.sleep(0)
                self._fresh(refs, check_memory=False)
                item.state = "applying"
                receipts[n + len(published)] = self._receipt(item)
                self.repository.save(jid, "processing", self.clock.now(), operations=receipts)
                write_item(self.memory, item)
                receipts[n + len(published)] = self._receipt(item)
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
                metrics={
                    "updated_files": len(receipts),
                    "proposals": len(items),
                    "usage": self.model.metrics,
                },
            )
            return {"outcome": "completed", "job_id": jid, "updated_files": len(writes)}
        except asyncio.CancelledError:
            self.repository.save(
                jid,
                "requires_reconciliation" if receipts else "pending",
                self.clock.now(),
                operations=receipts,
                metrics={"usage": self.model.metrics},
            )
            raise
        except (ReflectionError, OrganizationError, HistoryError, CoreMemoryError) as exc:
            code = getattr(exc, "code", "learning_storage_or_source")
            limited = code == "maintenance_budget"
            state = "requires_reconciliation" if receipts else ("pending" if limited else "failed")
            self.repository.save(
                jid,
                state,
                self.clock.now(),
                operations=receipts,
                error=code,
                metrics={"usage": self.model.metrics},
            )
            return {
                "outcome": "budget_exhausted" if limited else state,
                "job_id": jid,
                "error_code": code,
            }

    def _fresh(self, refs, *, check_memory=True) -> None:
        if "memory" in refs:
            for source in refs["memory"] if check_memory else []:
                with self.memory.io():
                    current = self.memory.raw(self.memory.path(source["path"]))
                if digest(current) != source["hash"]:
                    raise HistoryError("Curator memory changed before publication.")
            fresh, _, _ = self.repository.snapshot([r["id"] for r in refs["runs"]], [], self.policy)
            if fresh["runs"] != refs["runs"]:
                raise HistoryError("Curator evidence changed before publication.")
            return
        fresh, _, _ = self.repository.snapshot(
            [r["id"] for r in refs["runs"]],
            [e["id"] for e in refs["episodes"]],
            self.policy,
            floor=refs.get("run_floor", 0),
        )
        if fresh != refs:
            raise HistoryError("Learning sources changed before publication.")

    def recovery_ready(self) -> bool:
        return bool(
            self.policy.enabled
            and not self._lock.locked()
            and not self.repository.running()
            and self.repository.recovery_job(self.clock.now(), self.policy.recovery_attempts)
        )

    async def recover_background(self) -> dict:
        await asyncio.sleep(0)
        return self.recover_pending()

    def recover_pending(self) -> dict:
        """At most one bounded, tool/model-free receipt check per startup/idle cycle."""
        if not self.recovery_ready():
            return {"outcome": "no_recovery_work"}
        job = self.repository.recovery_job(self.clock.now(), self.policy.recovery_attempts)
        recovery = dict(job["metrics"].get("recovery", {}))
        recovery.update(
            attempts=recovery.get("attempts", 0) + 1,
            next_check_at=(
                self.clock.now() + timedelta(seconds=self.policy.recovery_interval_seconds)
            ).isoformat(),
        )
        self.repository.save(
            job["id"],
            job["state"],
            self.clock.now(),
            metrics={**job["metrics"], "recovery": recovery},
        )
        deadline = monotonic() + self.policy.recovery_seconds
        report = self.reconcile(job["id"], deadline=deadline)
        if report["outcome"] != "completed":
            report = self._replan_verified(
                self.repository.get(job["id"]), automatic=True, deadline=deadline
            )
        return {**report, "requests": 0}

    def reconcile(self, jid: str, *, deadline: float | None = None) -> dict:
        job = self.repository.get(jid)
        if job["state"] != "requires_reconciliation":
            return {"outcome": job["state"], "job_id": jid}
        operations = job["operations"]
        if len(operations) > 9 or not operations:
            self.repository.save(jid, job["state"], self.clock.now(), error="invalid_receipt_range")
            return {"outcome": job["state"], "job_id": jid}
        for op in operations:
            op["state"] = "uncertain"
            op["code"] = "recovery_time_budget"
        for op in operations:
            if deadline is not None and monotonic() >= deadline:
                break
            if op["path"] in {"persona.md", "MEMORY.md"} or op["path"].endswith("/MEMORY.md"):
                op["code"] = "learning_target_forbidden"
                continue
            try:
                with self.memory.io():
                    path = self.memory.path(op["path"])
                    limit = self.memory.max_bytes if op["path"] in BLOCKS else FILE_BYTES
                    current = digest(self.memory.raw(path, limit)) if path.exists() else None
                if current != op["after_hash"]:
                    if current == op["before_hash"] and (
                        current is not None or op["action"] == "create"
                    ):
                        op["state"], op["code"] = "not_applied", "verified_not_published"
                    else:
                        op["code"] = "content_changed"
                    continue
            except CoreMemoryError as error:
                op["code"] = error.code
                continue
            item = PlanItem(
                id=op["id"],
                candidate=None,
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
        error = next(
            (op["code"] for op in operations if op["state"] not in {"applied", "not_applied"}), ""
        )
        self.repository.save(jid, state, self.clock.now(), operations=operations, error=error)
        return {"outcome": state, "job_id": jid}

    def retry_job(self, jid: str) -> dict:
        """Explicit host retry: inspect every prior receipt before permitting fresh planning."""
        job = self.repository.get(jid)
        if job["state"] == "requires_reconciliation":
            verified = self.reconcile(jid)
            if verified["outcome"] == "completed":
                return verified
            return self._replan_verified(self.repository.get(jid), automatic=False)
        if job["state"] == "failed":
            self.repository.save(jid, "pending", self.clock.now())
        return {"outcome": self.repository.get(jid)["state"], "job_id": jid}

    def _replan_verified(
        self, job: dict, *, automatic: bool, deadline: float | None = None
    ) -> dict:
        jid = job["id"]
        if not job["operations"] or any(
            op["state"] not in {"applied", "not_applied"} for op in job["operations"]
        ):
            return {
                "outcome": "requires_reconciliation",
                "job_id": jid,
                "error_code": job["error"] or "unverified_receipts",
            }
        try:

            def check_time():
                if deadline is not None and monotonic() >= deadline:
                    raise ReflectionError("recovery_time_budget")

            check_time()
            self._fresh(job["sources"], check_memory=False)
            check_time()
            recovery = dict(job["metrics"].get("recovery", {}))
            published = [op for op in job["operations"] if op["state"] == "applied"]
            sources = job["sources"]
            if "memory" in sources:
                paths = list(
                    dict.fromkeys(
                        [r["path"] for r in sources["memory"]] + [op["path"] for op in published]
                    )
                )
                with self.memory.io():
                    refreshed = []
                    for p in paths:
                        check_time()
                        refreshed.append(
                            {
                                "path": p,
                                "hash": digest(
                                    self.memory.raw(
                                        self.memory.path(p),
                                        self.memory.max_bytes if p in BLOCKS else FILE_BYTES,
                                    )
                                ),
                            }
                        )
                    sources["memory"] = refreshed
            check_time()
            replans = recovery.get("replans", 0)
            limited = automatic and replans >= self.policy.recovery_attempts
            recovery.update(
                attempts=0, next_check_at=None, replans=replans + 1, published=published
            )
            self.repository.save(
                jid,
                "failed" if limited else "pending",
                self.clock.now(),
                operations=[],
                sources=sources,
                error="recovery_replan_limit" if limited else "",
                metrics={**job["metrics"], "recovery": recovery},
            )
            return {
                "outcome": "failed" if limited else "pending",
                "job_id": jid,
                "replanned_after_verified_receipts": not limited,
            }
        except (HistoryError, CoreMemoryError, ReflectionError) as error:
            code = getattr(error, "code", "recovery_source_changed")
            self.repository.save(jid, "requires_reconciliation", self.clock.now(), error=code)
            return {"outcome": "requires_reconciliation", "job_id": jid, "error_code": code}
