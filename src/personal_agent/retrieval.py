"""Adaptive lexical retrieval over derived topics, then bounded complete raw Runs."""

import re
from datetime import datetime

from personal_agent.core.retrieval import RetrievalItem, RetrievalResult
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
    def __init__(self, repository: ThreadRepository):
        self.repository = repository
        self.episodes: EpisodeRepository = repository.episodes

    def retrieve(self, user_input: str, exclude_run_ids: frozenset[str]) -> RetrievalResult:
        if not HISTORY.search(user_input[:1024]):
            return RetrievalResult()
        terms = keywords(user_input)
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
                rows = db.execute(
                    "SELECT e.id FROM episodes e JOIN conversations c ON c.id=e.stream_id "
                    "WHERE e.thread_id=? AND c.agent_id=? ORDER BY e.ended_at DESC,e.id LIMIT 3",
                    (thread.id, self.repository.agent_id),
                ).fetchall()
            else:
                # Bounded recent metadata fallback. Old rare topics remain accessible
                # through Recall instead of unbounded substring scans.
                episode_conditions = " OR ".join(
                    "instr(lower(json_extract(payload,'$.title')||' '||"
                    "json_extract(payload,'$.summary')),lower(?))>0"
                    for _ in terms
                )
                rows = db.execute(
                    "WITH recent AS MATERIALIZED (SELECT e.id,e.payload,e.ended_at FROM episodes e "
                    "JOIN conversations c ON c.id=e.stream_id WHERE c.agent_id=? "
                    "ORDER BY e.ended_at DESC,e.id LIMIT 200) "
                    "SELECT id FROM recent WHERE "
                    + episode_conditions
                    + " ORDER BY ended_at DESC,id LIMIT 3",
                    (self.repository.agent_id, *terms),
                ).fetchall()
            episodes = [self.episodes._episode(db, row[0]) for row in rows]
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
                        }
                    )
                )
            selected = set(exclude_run_ids)
            used = 0
            for episode in episodes:
                if len(items) >= 8 or used >= 2:
                    break
                rows = db.execute(
                    "SELECT r.rowid AS position,r.* FROM episode_runs er JOIN runs r "
                    "ON r.id=er.run_id AND r.conversation_id=er.stream_id "
                    "JOIN conversations c ON c.id=r.conversation_id "
                    "WHERE er.episode_id=? AND er.stream_id=? AND c.agent_id=? "
                    "AND r.status='completed' ORDER BY er.ordinal DESC LIMIT 2",
                    (episode.id, episode.stream_id, self.repository.agent_id),
                ).fetchall()
                for row in rows:
                    if row["id"] in selected or used >= 2:
                        continue
                    source = self.episodes._source(db, row, 65536)
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
