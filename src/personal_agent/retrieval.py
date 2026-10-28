"""Adaptive lexical retrieval over derived topics, then bounded complete raw Runs."""

import re
import sqlite3
from datetime import datetime
from time import monotonic

from personal_agent.core.retrieval import RetrievalItem, RetrievalPolicy, RetrievalResult
from personal_agent.memory.retrieval import (
    DEPENDENCY,
    PREFLIGHT,
    MemoryLookup,
    MemoryRetrievalPolicy,
    RetrievalChoice,
)
from personal_agent.memory.store import MemoryFS
from personal_agent.organization.models import Episode, Thread
from personal_agent.organization.provider import OrganizerModel
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.recall import query_budget
from personal_agent.storage.threads import ThreadRepository

HISTORY = re.compile(
    r"昨天|前天|上周|上个月|之前|上次|以前|还记得|那次|继续.{3,}|"
    r"\byesterday\b|\blast (?:week|month|time)\b|\bprevious\b|\bremember\b|"
    r"\b(?:resume|continue)\s+.{4,}",
    re.IGNORECASE,
)
STOP = re.compile(r"继续|昨天|前天|上周|上个月|之前|上次|以前|还记得|我们|那个|讨论|的")


def keywords(text: str) -> list[str]:
    terms = re.findall(r"[A-Za-z][A-Za-z0-9_.-]{1,63}|[\u4e00-\u9fff]{2,}", text[:512])
    output = []
    for term in terms:
        term = STOP.sub("", term)
        if len(term) < 2 or term.lower() in {"continue", "resume", "yesterday", "remember"}:
            continue
        output.append(term)
        if re.fullmatch(r"[\u4e00-\u9fff]+", term):
            output.extend(term[i : i + 2] for i in range(0, min(len(term) - 1, 8), 2))
    return list(dict.fromkeys(output))[:8]


class ContextRetriever:
    def __init__(
        self,
        repository: ThreadRepository,
        policy: RetrievalPolicy | None = None,
        *,
        memory: MemoryFS | None = None,
        memory_policy: MemoryRetrievalPolicy | None = None,
        preflight_model: OrganizerModel | None = None,
    ) -> None:
        self.repository = repository
        self.policy = policy or RetrievalPolicy()
        self.episodes: EpisodeRepository = repository.episodes
        if memory is not None and memory.root.name != repository.agent_id:
            raise ValueError("Agent Memory mismatch")
        self.lookup = MemoryLookup(memory, memory_policy) if memory else None
        self.preflight_model = preflight_model

    def _candidates(
        self, db: sqlite3.Connection, thread: Thread | None, terms: list[str]
    ) -> list[Episode]:
        ids = []
        if thread:
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT e.id FROM episodes e JOIN conversations c ON c.id=e.stream_id "
                    "WHERE e.thread_id=? AND c.agent_id=? ORDER BY e.ended_at DESC,e.id LIMIT ?",
                    (thread.id, self.repository.agent_id, self.policy.episode_limit),
                )
            ]
        # Even a Thread hit may lag behind the latest unassigned Episode. Rank
        # bounded recent metadata, without writing a relationship or asking an LLM.
        score_sql = "+".join(
            "(3*(instr(lower(title),lower(?))>0)+(instr(lower(summary),lower(?))>0))" for _ in terms
        )
        parameters = [term for term in terms for _ in range(2)]
        rows = db.execute(
            "WITH recent AS MATERIALIZED (SELECT e.id,e.ended_at,"
            "json_extract(e.payload,'$.title') AS title,"
            "json_extract(e.payload,'$.summary') AS summary "
            "FROM episodes e JOIN conversations c ON c.id=e.stream_id "
            "WHERE c.agent_id=? AND (? OR e.thread_id IS NULL) "
            "ORDER BY e.ended_at DESC,e.id LIMIT ?), "
            "ranked AS (SELECT id,ended_at," + score_sql + " AS match_score FROM recent) "
            "SELECT id FROM ranked WHERE match_score>0 "
            "ORDER BY match_score DESC,ended_at DESC,id LIMIT ?",
            (
                self.repository.agent_id,
                thread is None,
                self.policy.scan_episodes,
                *parameters,
                self.policy.episode_limit,
            ),
        ).fetchall()
        ids = list(dict.fromkeys([*ids, *(row[0] for row in rows)]))
        candidates = [self.episodes._episode(db, episode_id) for episode_id in ids]

        def score(episode: Episode) -> tuple[int, datetime, str]:
            match = sum(
                3 * (term.lower() in episode.title.lower())
                + (term.lower() in episode.summary.lower())
                for term in terms
            )
            return match, episode.ended_at, episode.id

        return sorted(candidates, key=score, reverse=True)[: self.policy.episode_limit]

    def _historical(
        self,
        user_input: str,
        exclude_run_ids: frozenset[str],
        *,
        thread_id: str | None = None,
        episode_ids: list[str] | None = None,
    ) -> RetrievalResult:
        if not thread_id and episode_ids is None and not HISTORY.search(user_input[:1024]):
            return RetrievalResult()
        terms = keywords(user_input) or (["history"] if thread_id or episode_ids else [])
        if not terms:
            return RetrievalResult(path="recall_fallback")
        with self.repository.store._transaction() as db, query_budget(db):
            # Match metadata in SQL, not a full Python deserialization scan. Literal
            # instr parameters do not expose FTS/LIKE query syntax to user input.
            conditions = " OR ".join(
                "instr(lower(json_extract(payload,'$.title')),lower(?))>0" for _ in terms
            )
            row = db.execute(
                "SELECT id FROM topic_threads WHERE agent_id=? AND (" + conditions + ") "
                "ORDER BY last_active_at DESC,id LIMIT 1",
                (self.repository.agent_id, *terms),
            ).fetchone()
            if thread_id:
                row = db.execute(
                    "SELECT id FROM topic_threads WHERE id=? AND agent_id=?",
                    (thread_id, self.repository.agent_id),
                ).fetchone()
            elif episode_ids is not None:
                row = None
            items = []
            thread = self.repository._thread(db, row[0]) if row else None
            if thread:
                items.append(
                    RetrievalItem(
                        reference={
                            "kind": "thread",
                            "id": thread.id,
                            "title": thread.title,
                            "revision": thread.revision,
                            "last_active_at": thread.last_active_at.isoformat(),
                            "status": thread.status,
                            "current_state": None if thread.dirty else thread.current_state,
                            "state_omitted_pending_revision": thread.dirty,
                        }
                    )
                )
            episodes = (
                [self.episodes._episode(db, eid) for eid in episode_ids]
                if episode_ids is not None
                else self._candidates(db, thread, terms)
            )
            if thread:
                items[0].reference["newer_unlinked_evidence"] = any(
                    e.thread_id is None and e.ended_at > thread.last_active_at for e in episodes
                )
            for episode in episodes:
                items.append(
                    RetrievalItem(
                        reference={
                            "kind": "episode",
                            "id": episode.id,
                            "stream_id": episode.stream_id,
                            "revision": episode.revision,
                            "started_at": episode.started_at.isoformat(),
                            "ended_at": episode.ended_at.isoformat(),
                            "title": episode.title,
                            "summary": episode.summary,
                            "status": episode.status,
                            "run_ids": episode.run_ids,
                            "thread_id": episode.thread_id,
                            "retrieved_via": "thread"
                            if thread and episode.thread_id == thread.id
                            else "lexical_supplement",
                        }
                    )
                )
            selected = set(exclude_run_ids)
            used = 0
            for episode in episodes:
                if len(items) >= 8 or used >= self.policy.raw_run_limit:
                    break
                rows = db.execute(
                    "SELECT r.rowid AS position,r.* FROM episode_runs er JOIN runs r "
                    "ON r.id=er.run_id AND r.conversation_id=er.stream_id "
                    "JOIN conversations c ON c.id=r.conversation_id "
                    "WHERE er.episode_id=? AND er.stream_id=? AND c.agent_id=? "
                    "AND r.status='completed' ORDER BY er.ordinal DESC LIMIT ?",
                    (
                        episode.id,
                        episode.stream_id,
                        self.repository.agent_id,
                        self.policy.raw_run_limit,
                    ),
                ).fetchall()
                for row in rows:
                    if row["id"] in selected or used >= self.policy.raw_run_limit:
                        continue
                    source = self.episodes._source(db, row, self.policy.max_run_bytes)
                    if not source.turn:
                        continue
                    items.append(
                        RetrievalItem(
                            reference={
                                "kind": "historical_run",
                                "episode_id": episode.id,
                                "stream_id": episode.stream_id,
                                "run_id": source.run_id,
                                "started_at": source.started_at.isoformat(),
                                "ended_at": source.ended_at.isoformat(),
                                "first_sequence": source.turn.first_sequence,
                                "last_sequence": source.turn.last_sequence,
                                "source_hash": source.turn.source_hash,
                            },
                            turn=source.turn,
                        )
                    )
                    selected.add(source.run_id)
                    used += 1
            # Present complete historical Runs in source order before recent dialogue.
            raw = [item for item in items if item.turn]
            metadata = [item for item in items if not item.turn]
            raw.sort(key=lambda item: datetime.fromisoformat(item.reference["started_at"]))
            return RetrievalResult(
                path="thread" if thread else ("episode" if episodes else "recall_fallback"),
                items=[*metadata, *raw],
            )

    def retrieve(self, user_input: str, exclude_run_ids: frozenset[str]) -> RetrievalResult:
        started = monotonic()
        result = self._historical(user_input, exclude_run_ids)
        if not self.lookup or not self.lookup.policy.enabled:
            return result
        if not DEPENDENCY.search(user_input[:1024]) and not result.items:
            result.metrics = {
                "mode": "fast",
                "memory_files": 0,
                "extra_requests": 0,
                "elapsed_seconds": monotonic() - started,
            }
            return result
        catalog = self.lookup.catalogue()
        topic = " ".join(
            str(item.reference.get("title", ""))
            for item in result.items
            if item.reference.get("kind") == "thread"
        )
        ranked = self.lookup.rank(catalog, user_input + " " + topic)
        memories = self.lookup.read(
            [r["path"] for _, r in ranked], catalog, user_input + " " + topic
        )
        reliable = bool(
            ranked and (len(ranked) == 1 or (ranked[0][0] >= 3 and ranked[0][0] > ranked[1][0]))
        )
        result.items = self._merge(memories, result.items)
        if memories:
            result.path = "lexical"
        elif result.path == "fast":
            result.path = "recall_fallback"
        candidates = []
        if not reliable and result.path in {"recall_fallback", "lexical"}:
            candidates.extend(
                [r for _, r in ranked]
                + [r for r in catalog if r["path"] not in {row["path"] for _, row in ranked}]
            )
            candidates = candidates[: max(1, self.lookup.policy.candidates - 4)]
            for thread in self.repository.list(limit=2):
                candidates.append(
                    {
                        "id": "thread:" + thread.id,
                        "kind": "thread",
                        "title": thread.title,
                        "source_id": thread.id,
                    }
                )
            with self.repository.store._transaction() as db:
                rows = db.execute(
                    "SELECT e.id,json_extract(e.payload,'$.title') AS title "
                    "FROM episodes e JOIN conversations c ON c.id=e.stream_id "
                    "WHERE c.agent_id=? ORDER BY e.ended_at DESC,e.id LIMIT 2",
                    (self.repository.agent_id,),
                ).fetchall()
            candidates.extend(
                {
                    "id": "episode:" + r["id"],
                    "kind": "episode",
                    "source_id": r["id"],
                    "title": r["title"],
                }
                for r in rows
            )
        result.candidates = candidates[: self.lookup.policy.candidates]
        result.metrics = {
            "mode": "lexical" if result.items else "recall_fallback",
            "memory_files": len(memories),
            "extra_requests": 0,
            "elapsed_seconds": monotonic() - started,
        }
        return result

    def _merge(self, memories, history):
        return [*memories, *history][:8]

    def _supplement(self, baseline, additions):
        """Existing evidence has priority over optional model-selected additions."""
        selected, seen = [], set()
        memory_files, raw_runs = 0, 0
        for item in [*baseline, *additions]:
            ref = item.reference
            semantic = ref.get("kind") == "semantic_memory"
            key = (
                ("run", item.turn.run_id)
                if item.turn
                else (ref.get("kind"), ref.get("path") or ref.get("id"))
            )
            if key in seen or len(selected) >= 8:
                continue
            if semantic and memory_files >= self.lookup.policy.file_limit:
                continue
            if item.turn and raw_runs >= self.policy.raw_run_limit:
                continue
            selected.append(item)
            seen.add(key)
            memory_files += semantic
            raw_runs += item.turn is not None
        return selected

    async def preflight(
        self, user_input: str, exclude_run_ids: frozenset[str], result: RetrievalResult
    ) -> RetrievalResult:
        started = monotonic()
        baseline = result.model_copy(deep=True)
        try:
            return await self._preflight(user_input, exclude_run_ids, result.model_copy(deep=True))
        except Exception:
            requests = (
                self.preflight_model.metrics.get("requests", 0) if self.preflight_model else 0
            )
            baseline.path = baseline.path if baseline.items else "recall_fallback"
            baseline.metrics.update(
                {
                    "mode": baseline.path,
                    "extra_requests": requests,
                    "memory_files": sum(
                        i.reference.get("kind") == "semantic_memory" for i in baseline.items
                    ),
                    "error_code": "preflight_unavailable",
                    "preflight_usage": self.preflight_model.metrics if self.preflight_model else {},
                    "elapsed_seconds": float(result.metrics.get("elapsed_seconds", 0))
                    + monotonic()
                    - started,
                },
            )
            return baseline

    async def _preflight(
        self, user_input: str, exclude_run_ids: frozenset[str], result: RetrievalResult
    ) -> RetrievalResult:
        if (
            not self.lookup
            or not self.lookup.policy.llm_preflight
            or not self.preflight_model
            or not result.candidates
        ):
            return result
        started = monotonic()
        choice = await self.preflight_model.ask(
            RetrievalChoice,
            PREFLIGHT,
            {"question": user_input[:2048], "candidates": result.candidates},
        )
        known = {row["id"]: row for row in result.candidates}
        if len(set(choice.ids)) != len(choice.ids) or any(cid not in known for cid in choice.ids):
            raise ValueError("Unknown retrieval choice")
        chosen = [known[cid] for cid in choice.ids]
        # Re-read fresh indexed catalogue and Agent-scoped history after model await.
        catalog = self.lookup.catalogue()
        memories = self.lookup.read(
            [row["path"] for row in chosen if row["kind"] == "memory"], catalog, user_input
        )
        threads = [r["source_id"] for r in chosen if r["kind"] == "thread"]
        episodes = [r["source_id"] for r in chosen if r["kind"] == "episode"]
        history = (
            self._historical(
                user_input,
                exclude_run_ids,
                thread_id=threads[0] if threads else None,
                episode_ids=episodes if episodes else None,
            ).items
            if threads or episodes
            else []
        )
        additions = self._merge(memories, history)
        result.items = self._supplement(result.items, additions)
        result.path = (
            "llm_preflight" if additions else (result.path if result.items else "recall_fallback")
        )
        result.metrics.update(
            {
                "mode": result.path,
                "memory_files": sum(
                    i.reference.get("kind") == "semantic_memory" for i in result.items
                ),
                "extra_requests": 1,
                "preflight_usage": self.preflight_model.metrics,
                "elapsed_seconds": float(result.metrics.get("elapsed_seconds", 0))
                + monotonic()
                - started,
            }
        )
        return result
