"""SELECT-only, Agent-bound projections over the existing owner's SQLite connection."""

import json

from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.storage.threads import ThreadRepository


class NotFound(Exception):
    pass


def page(rows: list[dict], limit: int) -> dict:
    more = len(rows) > limit
    items = rows[:limit]
    cursor = items[-1]["position"] if more and items else None
    for item in items:
        item.pop("position", None)
    return {"items": items, "next_cursor": cursor, "has_more": more}


class ObservatoryRepository:
    def __init__(self, store: SQLiteStore, agent_id: str) -> None:
        self.store = store
        self.agent_id = agent_id
        self.episodes = EpisodeRepository(store, agent_id)
        self.threads = ThreadRepository(store, agent_id)

    def listing(
        self,
        kind: str,
        limit: int = 8,
        cursor: int = 9223372036854775807,
        *,
        thread_id: str | None = None,
        episode_id: str | None = None,
    ) -> dict:
        # Identifiers come exclusively from this fixed map, never URL interpolation.
        sources = {
            "runs": ("runs", "conversation_id", "id,status,started_at,ended_at,conversation_id"),
            "episodes": (
                "episodes",
                "stream_id",
                "id,status,started_at,ended_at,revision,thread_id",
            ),
            "threads": ("topic_threads", None, "id,revision,dirty,last_active_at"),
            "jobs": ("memory_learning_jobs", None, "id,kind,state,created_at,updated_at,error"),
        }
        table, owner, columns = sources[kind]
        scope = (
            f"t.{owner} IN (SELECT id FROM conversations WHERE agent_id=?)"
            if owner
            else "t.agent_id=?"
        )
        columns = ",".join("t." + name for name in columns.split(","))
        if kind in {"threads", "episodes"}:
            columns += ",substr(json_extract(t.payload,'$.title'),1,120) AS title"
        if kind == "threads":
            columns += ",json_extract(t.payload,'$.status') AS status"
        params: list = [self.agent_id, cursor]
        extra = ""
        if thread_id is not None:
            self.detail("threads", thread_id)
            extra = " AND t.thread_id=?"
            params.append(thread_id)
        if episode_id is not None:
            self.detail("episodes", episode_id)
            extra = " AND t.id IN (SELECT run_id FROM episode_runs WHERE episode_id=?)"
            params.append(episode_id)
        with self.store.reading() as db:
            rows = db.execute(
                f"SELECT t.rowid AS position,{columns} FROM {table} t WHERE {scope} "
                f"AND t.rowid<?{extra} ORDER BY t.rowid DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        return page([dict(r) for r in rows], limit)

    def detail(self, kind: str, object_id: str) -> dict:
        with self.store.reading() as db:
            if kind == "threads":
                row = db.execute(
                    "SELECT id FROM topic_threads WHERE id=? AND agent_id=?",
                    (object_id, self.agent_id),
                ).fetchone()
                if row:
                    return self.threads.read_thread(db, object_id).model_dump(mode="json")
            elif kind == "episodes":
                row = db.execute(
                    "SELECT e.id FROM episodes e JOIN conversations c ON c.id=e.stream_id "
                    "WHERE e.id=? AND c.agent_id=?",
                    (object_id, self.agent_id),
                ).fetchone()
                if row:
                    return self.episodes.read_episode(db, object_id).model_dump(mode="json")
            elif kind == "jobs":
                row = db.execute(
                    "SELECT id,kind,state,created_at,updated_at,error,sources,operations,"
                    "substr(metrics,1,16384) AS metrics FROM memory_learning_jobs "
                    "WHERE id=? AND agent_id=?",
                    (object_id, self.agent_id),
                ).fetchone()
                if row:
                    result = dict(row)
                    for key in ("sources", "operations", "metrics"):
                        result[key] = json.loads(result[key])
                    return result
            elif kind == "runs":
                row = db.execute(
                    "SELECT r.id,r.status,r.started_at,r.ended_at,r.conversation_id,"
                    "json_extract(r.result_json,'$.status') AS outcome,"
                    "json_extract(r.result_json,'$.rounds') AS rounds "
                    "FROM runs r JOIN conversations c ON c.id=r.conversation_id "
                    "WHERE r.id=? AND c.agent_id=?",
                    (object_id, self.agent_id),
                ).fetchone()
                if row:
                    return dict(row)
        raise NotFound

    def messages(self, run_id: str, limit: int, after: int) -> dict:
        self.detail("runs", run_id)
        with self.store.reading() as db:
            # Do not fetch opaque provider JSON/reasoning, even for an explicit reveal.
            rows = db.execute(
                "SELECT m.sequence AS position,m.sequence,m.created_at,"
                "json_extract(m.payload,'$.role') AS role,"
                "substr(json_extract(m.payload,'$.text'),1,8192) AS text,"
                "substr(json_extract(m.payload,'$.tool_calls'),1,16384) AS tool_calls,"
                "substr(json_extract(m.payload,'$.result'),1,16384) AS result,"
                "length(CAST(m.payload AS BLOB))>16384 AS payload_large "
                "FROM messages m JOIN runs r ON r.id=m.run_id "
                "JOIN conversations c ON c.id=r.conversation_id "
                "WHERE m.run_id=? AND c.agent_id=? AND m.sequence>? "
                "ORDER BY m.sequence LIMIT ?",
                (run_id, self.agent_id, after, limit + 1),
            ).fetchall()
        items = [dict(r) for r in rows]
        for item in items:
            for key in ("tool_calls", "result"):
                if item[key]:
                    try:
                        item[key] = json.loads(item[key])
                    except ValueError:
                        item[key] = {"truncated": True, "preview": item[key]}
        return page(items, limit)

    def schedule(self) -> dict | None:
        with self.store.reading() as db:
            row = db.execute(
                "SELECT stream_id,last_activity_at,thread_due_at,cooldown_until,last_cycle_at,"
                "last_error,last_batches,last_requests,last_episode_success_at,"
                "last_thread_success_at,last_repair_success_at "
                "FROM organization_schedule WHERE agent_id=?",
                (self.agent_id,),
            ).fetchone()
        return dict(row) if row else None

    def character_receipts(self, scope: tuple, limit: int, cursor: int) -> dict:
        with self.store.reading() as db:
            rows = db.execute(
                "SELECT s.rowid AS position,s.episode_id,s.episode_revision,s.state,s.error,"
                "s.source_hash FROM character_state_sources s JOIN episodes e ON e.id=s.episode_id "
                "JOIN conversations c ON c.id=e.stream_id "
                "WHERE s.agent_id=? AND s.character_id=? AND s.package_hash=? AND c.agent_id=? "
                "AND s.rowid<? ORDER BY s.rowid DESC LIMIT ?",
                (*scope, self.agent_id, cursor, limit + 1),
            ).fetchall()
        return page([dict(r) for r in rows], limit)

    def file_jobs(self, path: str, limit: int, cursor: int) -> dict:
        with self.store.reading() as db:
            rows = db.execute(
                "SELECT j.rowid AS position,j.id,j.kind,j.state,j.updated_at "
                "FROM memory_learning_jobs j WHERE j.agent_id=? AND j.rowid<? "
                "AND EXISTS (SELECT 1 FROM json_each(j.operations) o "
                "WHERE json_extract(o.value,'$.path')=?) ORDER BY j.rowid DESC LIMIT ?",
                (self.agent_id, cursor, path, limit + 1),
            ).fetchall()
        return page([dict(r) for r in rows], limit)
