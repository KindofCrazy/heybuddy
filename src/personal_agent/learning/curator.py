"""Low-frequency, bounded curation; reuse the learner's publication and reconciliation path."""

import re
from datetime import datetime, timedelta

from pydantic import Field

from personal_agent.core.compaction import redact
from personal_agent.core.models import Model
from personal_agent.learning.service import MemoryLearningService
from personal_agent.memory.models import digest
from personal_agent.reflection.memory import catalogue, plan_item, read_comparison
from personal_agent.reflection.models import (
    Candidate,
    Decision,
    Evidence,
    Extraction,
    ReflectionError,
)
from personal_agent.reflection.service import BASE


class CuratorPolicy(Model):
    enabled: bool = True
    interval_seconds: float = Field(default=86400, gt=0, le=2592000)
    update_threshold: int = Field(default=12, ge=1, le=1000)
    long_file_bytes: int = Field(default=12000, ge=1024, le=32768)
    max_files: int = Field(default=3, ge=1, le=3)
    max_bytes: int = Field(default=65536, ge=4096, le=131072)


class CuratorChange(Decision):
    kind: str = Field(pattern="^(deduplicate|organization|reconcile|split)$")
    evidence: list[Evidence] = Field(default_factory=list, max_length=8)
    source_path: str = Field(default="", max_length=260)
    source_text: str = Field(default="", max_length=8192)


class Curation(Model):
    changes: list[CuratorChange] = Field(max_length=3)


CURATE = BASE + (
    "Curate only the supplied small group of Markdown files. Deduplicate literal repeated facts, "
    "preserve stable preferences regardless of age, and organize headings WITHOUT losing claims. "
    "Unresolved conflicts => no_op; never resolve by guessing or by age alone. Reconciliation of "
    "Obsolete state requires supplied USER evidence. No persona/index edits or file deletion. "
    "An empty changes list is valid. No-op when already tidy. One exact unique update per file. "
    "deduplicate/organization must preserve non-heading lines verbatim somewhere in the retained "
    "files; use reconcile with evidence when changing factual state. Limited split: create a new "
    "topic with frontmatter, source_path and an EXACT source_text span copied into new_text; then "
    "update the old file replacing that span with a root-relative Markdown link. Source content "
    "must remain discoverable before removal. At most three changes, all within max_files total. "
    "Only create as part of a split. Return changes using candidate_id 1..N, kind, action, path, "
    "old_text,new_text,reason,evidence,source_path,source_text."
)


def facts(text: str) -> set[str]:
    """Literal conservation guard for structural edits; semantic changes require user evidence."""
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "---", "name:", "description:"))
    }


class MemoryCurator:
    def __init__(self, learning: MemoryLearningService, policy: CuratorPolicy | None = None):
        self.learning = learning
        self.repository = learning.repository
        self.memory = learning.memory
        self.model = learning.model
        self.clock = learning.clock
        self.policy = policy or CuratorPolicy()
        if learning.policy.enabled:
            state = self.repository.activation()
            if state["curator_due_at"] is None:
                self._schedule()

    def _schedule(self, *, completed=False):
        with self.repository.store._transaction() as db:
            db.execute(
                "UPDATE memory_learning_state SET curator_due_at=?,"
                "curator_updates=CASE WHEN ? THEN updates ELSE curator_updates END "
                "WHERE agent_id=?",
                (
                    (
                        self.clock.now() + timedelta(seconds=self.policy.interval_seconds)
                    ).isoformat(),
                    completed,
                    self.repository.agent_id,
                ),
            )

    def _latest(self):
        with self.repository.store._transaction() as db:
            row = db.execute(
                "SELECT id FROM memory_learning_jobs WHERE agent_id=? AND kind='curator' "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                (self.repository.agent_id,),
            ).fetchone()
        return self.repository.get(row[0]) if row else None

    def _files(self):
        catalog = catalogue(self.memory)
        topics = [r for r in catalog if not r["path"].endswith("/MEMORY.md")]
        # Recent write paths anchor a small related set; oversized topics are also candidates.
        with self.repository.store._transaction() as db:
            rows = db.execute(
                "SELECT operations FROM memory_learning_jobs WHERE agent_id=? "
                "AND kind!='curator' AND state='completed' ORDER BY updated_at DESC "
                "LIMIT 4",
                (self.repository.agent_id,),
            ).fetchall()
        import json

        recent = [op["path"] for row in rows for op in json.loads(row[0])]
        anchor = next((r for r in topics if r["path"] in recent), topics[0] if topics else None)
        terms = set(
            re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2}", (str(anchor) if anchor else "").lower())
        )
        topics.sort(
            key=lambda row: (row["path"] in recent, sum(t in str(row).lower() for t in terms)),
            reverse=True,
        )
        chosen = topics[: max(1, self.policy.max_files - 1)]
        known = {r["path"] for r in catalog}
        comparison = read_comparison(self.memory, [r["path"] for r in chosen], known)
        comparison = {p: t for p, t in comparison.items() if p not in {"persona.md", "MEMORY.md"}}
        if len(comparison) > self.policy.max_files:
            comparison.pop("human.md", None)
        if sum(len(t.encode()) for t in comparison.values()) > self.policy.max_bytes:
            raise ReflectionError("curator_input_budget")
        return catalog, comparison

    def due(self):
        if (
            not self.policy.enabled
            or not self.learning.policy.enabled
            or self.repository.unresolved()
        ):
            return False
        latest = self._latest()
        if latest and latest["state"] in {"failed", "requires_reconciliation"}:
            return False
        state = self.repository.activation()
        if self.clock.now() < datetime.fromisoformat(state["curator_due_at"]):
            return False
        if state["updates"] - state["curator_updates"] >= self.policy.update_threshold:
            return True
        _, comparison = self._files()
        if any(len(text.encode()) >= self.policy.long_file_bytes for text in comparison.values()):
            return True
        self._schedule()
        return False

    def _evidence(self):
        # Reconciliation can cite only actual recent learning sources, not Memory text itself.
        with self.repository.store._transaction() as db:
            rows = db.execute(
                "SELECT sources FROM memory_learning_jobs WHERE agent_id=? "
                "AND kind!='curator' AND state='completed' ORDER BY updated_at DESC "
                "LIMIT 2",
                (self.repository.agent_id,),
            ).fetchall()
        import json

        ids = list(dict.fromkeys(r["id"] for row in rows for r in json.loads(row[0])["runs"]))[:8]
        _, records, _ = self.repository.snapshot(ids, [], self.learning.policy)
        return records

    async def run(self, *, force=False, retry=False):
        if not self.policy.enabled or not self.learning.policy.enabled:
            return {"outcome": "disabled"}
        if self.repository.unresolved():
            return {"outcome": "requires_reconciliation"}
        if not force and not self.due():
            return {"outcome": "no_curator_work"}
        async with self.learning._lock:
            catalog, comparison = self._files()
            refs = {
                "runs": [],
                "episodes": [],
                "memory": [{"path": p, "hash": digest(t.encode())} for p, t in comparison.items()],
            }
            latest = self._latest()
            known_hashes = {
                row["path"]: row["hash"]
                for row in (latest["sources"].get("memory", []) if latest else [])
            }
            if latest:
                known_hashes.update(
                    {
                        op["path"]: op["after_hash"]
                        for op in latest["operations"]
                        if op["state"] == "applied"
                    }
                )
            if (
                latest
                and latest["state"] == "completed"
                and all(known_hashes.get(row["path"]) == row["hash"] for row in refs["memory"])
            ):
                self._schedule(completed=True)
                return {"outcome": "completed", "updated_files": 0, "unchanged": True}
            if latest and latest["state"] == "failed" and not retry:
                return {"outcome": "failed", "error_code": latest["error"]}
            jid = (
                latest["id"]
                if latest and latest["state"] in {"pending", "failed"} and latest["sources"] == refs
                else self.repository.create("curator", refs, self.clock.now())
            )
            records = self._evidence()

            async def propose():
                if not comparison:
                    return []
                history = [row for cid, s in records for row in self.model._evidence(cid, [s.turn])]
                result = await self.model._ask(
                    Curation,
                    CURATE,
                    {
                        "memory": {p: redact(t) for p, t in comparison.items()},
                        "catalogue": catalog,
                        "history": history,
                        "max_files": self.policy.max_files,
                    },
                )
                return self._validate(result, comparison, catalog, records)

            report = await self.learning._execute(
                jid, refs, records, [], retry=retry, proposer=propose
            )
            if report["outcome"] == "completed":
                self._schedule(completed=True)
            return report

    def _validate(self, result, comparison, catalog, records):
        changes = result.changes
        targets = [c.path for c in changes if c.action in {"create", "update"}]
        if (
            len(set(targets)) != len(targets)
            or len(set(comparison) | set(targets)) > self.policy.max_files
        ):
            raise ReflectionError("curator_file_budget")
        creates = [c for c in changes if c.action == "create"]
        future = dict(comparison)
        for change in changes:
            if change.action == "create":
                future[change.path] = change.new_text
            elif change.action == "update" and change.path in future:
                future[change.path] = future[change.path].replace(
                    change.old_text, change.new_text, 1
                )
        items = []
        for change in sorted(changes, key=lambda c: c.action != "create"):
            if change.action not in {"create", "update"}:
                continue
            if change.path in {"persona.md", "MEMORY.md"} or change.path.endswith("/MEMORY.md"):
                raise ReflectionError("curator_target_forbidden")
            if change.action == "create" and change.kind != "split":
                raise ReflectionError("unsafe_memory_split")
            if change.kind == "reconcile":
                if not change.evidence:
                    raise ReflectionError("invalid_evidence")
                evidence = Candidate(
                    category="correction",
                    subject="curation",
                    content="current state",
                    reason=change.reason,
                    confidence="confirmed",
                    evidence=change.evidence,
                )
                self.learning._verify(Extraction(candidates=[evidence]), records)
            elif change.action == "create":
                if (
                    change.kind != "split"
                    or change.source_path not in comparison
                    or not change.source_text
                    or comparison[change.source_path].count(change.source_text) != 1
                    or change.source_text not in change.new_text
                ):
                    raise ReflectionError("unsafe_memory_split")
            elif change.kind == "split":
                destinations = [
                    c
                    for c in creates
                    if c.source_path == change.path
                    and c.source_text in change.old_text
                    and f"]({c.path})" in change.new_text
                ]
                if not destinations:
                    raise ReflectionError("split_destination_required")
                retained = "\n".join([change.new_text, *[c.new_text for c in destinations]])
                if not facts(change.old_text) <= facts(retained):
                    raise ReflectionError("curator_content_loss")
            else:
                retained = "\n".join(future.values())
                if not facts(change.old_text) <= facts(retained):
                    raise ReflectionError("curator_content_loss")
            decision = Decision(**change.model_dump(include=set(Decision.model_fields)))
            items.append(
                plan_item(self.memory, len(items) + 1, None, decision, comparison, catalog)
            )
        return items

    def status(self):
        if not self.learning.policy.enabled:
            return {"enabled": False, "due": False}
        latest = self._latest()
        return {
            "enabled": self.policy.enabled,
            "due": self.due(),
            "next_check_at": self.repository.activation()["curator_due_at"],
            "last": {k: latest[k] for k in ("id", "state", "updated_at", "error", "metrics")}
            if latest
            else None,
        }
