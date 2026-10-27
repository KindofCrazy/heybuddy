"""Learning task metadata only: references, hashes and publication receipts, no fact bodies."""

import json
from datetime import datetime
from uuid import uuid4

from personal_agent.core.history import HistoryError
from personal_agent.learning.models import LearningPolicy
from personal_agent.storage.episodes import EpisodeRepository

SCHEMA = (
    """CREATE TABLE memory_learning_jobs (
        id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id), kind TEXT NOT NULL,
        state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        sources TEXT NOT NULL CHECK(length(CAST(sources AS BLOB))<=16384),
        operations TEXT NOT NULL DEFAULT '[]' CHECK(length(CAST(operations AS BLOB))<=16384),
        error TEXT NOT NULL DEFAULT '', metrics TEXT NOT NULL DEFAULT '{}')""",
    "CREATE INDEX learning_job_agent ON memory_learning_jobs(agent_id,kind,state,created_at)",
    """CREATE TABLE memory_learning_sources (
        agent_id TEXT NOT NULL REFERENCES agents(id), kind TEXT NOT NULL, source_id TEXT NOT NULL,
        revision INTEGER NOT NULL, job_id TEXT NOT NULL REFERENCES memory_learning_jobs(id),
        PRIMARY KEY(agent_id,kind,source_id,revision))""",
    """CREATE TABLE memory_learning_state (
        agent_id TEXT PRIMARY KEY REFERENCES agents(id), activated_at TEXT NOT NULL,
        run_floor INTEGER NOT NULL, episode_floor INTEGER NOT NULL,
        priority_cursor INTEGER NOT NULL, updates INTEGER NOT NULL DEFAULT 0,
        curator_due_at TEXT, curator_updates INTEGER NOT NULL DEFAULT 0)""",
)


class LearningRepository:
    def __init__(self, episodes: EpisodeRepository):
        self.episodes = episodes
        self.store = episodes.store
        self.agent_id = episodes.agent_id

    def snapshot(self, run_ids: list[str], episode_ids: list[str], policy: LearningPolicy):
        """Resolve references afresh, Agent-bound, bounded before deserialization."""
        if len(set(run_ids)) != len(run_ids) or len(episode_ids) > policy.batch_episodes:
            raise HistoryError("Invalid learning sources.")
        with self.store._transaction() as db:
            episode_refs = []
            hints = []
            for eid in episode_ids:
                episode = self.episodes._episode(db, eid)
                if episode.status != "finalized":
                    raise HistoryError("Learning requires stable Episodes.")
                episode_refs.append({"id": eid, "revision": episode.revision})
                hints.append({"id": eid, "title": episode.title, "summary": episode.summary})
                run_ids = [*run_ids, *episode.run_ids]
            run_ids = list(dict.fromkeys(run_ids))
            if len(run_ids) > policy.max_source_runs:
                raise HistoryError("Learning source count exceeds budget.")
            records = []
            used = 0
            for rid in run_ids:
                row = db.execute(
                    "SELECT r.rowid AS position,r.* FROM runs r JOIN conversations c "
                    "ON c.id=r.conversation_id WHERE r.id=? AND c.agent_id=?",
                    (rid, self.agent_id),
                ).fetchone()
                if row is None:
                    raise HistoryError("Learning source inaccessible.")
                source = self.episodes._source(db, row, policy.source_bytes - used)
                if source.turn is None:
                    raise HistoryError("Learning source incomplete or oversized.")
                used += source.payload_bytes
                records.append((row["conversation_id"], source))
            records.sort(key=lambda pair: (pair[1].started_at, pair[1].position))
            refs = {
                "runs": [
                    {"id": s.run_id, "hash": s.turn.source_hash, "conversation_id": cid}
                    for cid, s in records
                ],
                "episodes": episode_refs,
            }
            return refs, records, hints

    def create(self, kind: str, refs: dict, now: datetime) -> str:
        jid = uuid4().hex
        with self.store._transaction() as db:
            for source_kind, rows in (("run", refs["runs"]), ("episode", refs["episodes"])):
                if kind == "batch" and source_kind == "run":
                    continue
                for row in rows:
                    found = db.execute(
                        "SELECT j.id,j.state FROM memory_learning_sources s JOIN "
                        "memory_learning_jobs j ON j.id=s.job_id WHERE s.agent_id=? "
                        "AND s.kind=? AND s.source_id=? AND s.revision=?",
                        (self.agent_id, source_kind, row["id"], row.get("revision", 0)),
                    ).fetchone()
                    if found:
                        return found["id"]
            db.execute(
                "INSERT INTO memory_learning_jobs "
                "(id,agent_id,kind,state,created_at,updated_at,sources) VALUES (?,?,?,?,?,?,?)",
                (
                    jid,
                    self.agent_id,
                    kind,
                    "pending",
                    now.isoformat(),
                    now.isoformat(),
                    json.dumps(refs),
                ),
            )
            for source_kind, rows in (("run", refs["runs"]), ("episode", refs["episodes"])):
                if kind == "batch" and source_kind == "run":
                    continue
                for row in rows:
                    db.execute(
                        "INSERT INTO memory_learning_sources VALUES (?,?,?,?,?)",
                        (self.agent_id, source_kind, row["id"], row.get("revision", 0), jid),
                    )
        return jid

    def get(self, jid: str) -> dict:
        with self.store._transaction() as db:
            row = db.execute(
                "SELECT * FROM memory_learning_jobs WHERE id=? AND agent_id=?", (jid, self.agent_id)
            ).fetchone()
            if row is None:
                raise HistoryError("Learning job inaccessible.")
            result = dict(row)
            for key in ("sources", "operations", "metrics"):
                result[key] = json.loads(result[key])
            return result

    def save(self, jid: str, state: str, now: datetime, *, operations=None, error="", metrics=None):
        old = self.get(jid)
        with self.store._transaction() as db:
            changed = db.execute(
                "UPDATE memory_learning_jobs SET state=?,updated_at=?,operations=?,"
                "error=?,metrics=? "
                "WHERE id=? AND agent_id=? AND state=? AND updated_at=?",
                (
                    state,
                    now.isoformat(),
                    json.dumps(operations if operations is not None else old["operations"]),
                    error,
                    json.dumps(metrics if metrics is not None else old["metrics"]),
                    jid,
                    self.agent_id,
                    old["state"],
                    old["updated_at"],
                ),
            ).rowcount
            if changed != 1:
                raise HistoryError("Learning job changed.")
            if state == "completed" and old["state"] != "completed":
                db.execute(
                    "UPDATE memory_learning_state SET updates=updates+? WHERE agent_id=?",
                    (
                        sum(
                            op["state"] == "applied"
                            for op in (operations if operations is not None else old["operations"])
                        ),
                        self.agent_id,
                    ),
                )

    def recover(self, now: datetime):
        with self.store._transaction() as db:
            db.execute(
                "UPDATE memory_learning_jobs SET state=CASE WHEN operations='[]' "
                "THEN 'pending' ELSE 'requires_reconciliation' END,updated_at=? "
                "WHERE agent_id=? AND state='processing'",
                (now.isoformat(), self.agent_id),
            )
