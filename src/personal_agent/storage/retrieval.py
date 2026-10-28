"""Agent-scoped bounded SQL snapshots for working-context retrieval."""

import sqlite3
from datetime import datetime

from personal_agent.core.retrieval import RetrievalItem, RetrievalPolicy, RetrievalResult
from personal_agent.organization.models import Episode, Thread
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.recall import query_budget
from personal_agent.storage.threads import ThreadRepository


class WorkingContextRepository:
    def __init__(self, repository: ThreadRepository, policy: RetrievalPolicy) -> None:
        self.repository = repository
        self.policy = policy
        self.episodes: EpisodeRepository = repository.episodes

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
        candidates = [self.episodes.read_episode(db, episode_id) for episode_id in ids]

        def score(episode: Episode) -> tuple[int, datetime, str]:
            match = sum(
                3 * (term.lower() in episode.title.lower())
                + (term.lower() in episode.summary.lower())
                for term in terms
            )
            return match, episode.ended_at, episode.id

        return sorted(candidates, key=score, reverse=True)[: self.policy.episode_limit]

    def history(
        self,
        terms: list[str],
        exclude_run_ids: frozenset[str],
        *,
        thread_id: str | None = None,
        episode_ids: list[str] | None = None,
    ) -> RetrievalResult:
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
            thread = self.repository.read_thread(db, row[0]) if row else None
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
                [self.episodes.read_episode(db, eid) for eid in episode_ids]
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
                    source = self.episodes.read_source(db, row, self.policy.max_run_bytes)
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

    def recent_episode_choices(self) -> list[dict]:
        with self.repository.store.reading() as db:
            rows = db.execute(
                "SELECT e.id,json_extract(e.payload,'$.title') AS title "
                "FROM episodes e JOIN conversations c ON c.id=e.stream_id "
                "WHERE c.agent_id=? ORDER BY e.ended_at DESC,e.id LIMIT 2",
                (self.repository.agent_id,),
            ).fetchall()
        return [
            dict(id="episode:" + r["id"], kind="episode", source_id=r["id"], title=r["title"])
            for r in rows
        ]
