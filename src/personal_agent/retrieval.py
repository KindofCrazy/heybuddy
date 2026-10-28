"""Adaptive lexical retrieval over derived topics, then bounded complete raw Runs."""

import re
from time import monotonic

from personal_agent.core.retrieval import RetrievalPolicy, RetrievalResult
from personal_agent.memory.retrieval import (
    DEPENDENCY,
    PREFLIGHT,
    MemoryLookup,
    MemoryRetrievalPolicy,
    RetrievalChoice,
)
from personal_agent.memory.store import MemoryFS
from personal_agent.organization.provider import OrganizerModel
from personal_agent.storage.retrieval import WorkingContextRepository
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
        self.history = WorkingContextRepository(repository, self.policy)
        if memory is not None and memory.root.name != repository.agent_id:
            raise ValueError("Agent Memory mismatch")
        self.lookup = MemoryLookup(memory, memory_policy) if memory else None
        self.preflight_model = preflight_model

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
        return self.history.history(
            terms, exclude_run_ids, thread_id=thread_id, episode_ids=episode_ids
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
            candidates.extend(self.history.recent_episode_choices())
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
