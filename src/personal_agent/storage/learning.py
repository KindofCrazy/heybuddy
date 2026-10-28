"""Learning task metadata only: references, hashes and publication receipts, no fact bodies."""

import json
import re
from datetime import datetime, timedelta
from uuid import uuid4

from personal_agent.core.history import HistoryError
from personal_agent.learning.models import LearningPolicy
from personal_agent.storage.episodes import EpisodeRepository
from personal_agent.storage.recall import query_budget

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
    def __init__(self, episodes: EpisodeRepository) -> None:
        self.episodes = episodes
        self.store = episodes.store
        self.agent_id = episodes.agent_id

    def snapshot(
        self,
        run_ids: list[str],
        episode_ids: list[str],
        policy: LearningPolicy,
        *,
        floor=0,
        byte_limit: int | None = None,
    ) -> tuple[dict, list, list]:
        """Resolve references afresh, Agent-bound, bounded before deserialization."""
        if len(set(run_ids)) != len(run_ids) or len(episode_ids) > policy.batch_episodes:
            raise HistoryError("Invalid learning sources.")
        with self.store._transaction() as db:
            episode_refs = []
            hints = []
            for eid in episode_ids:
                episode = self.episodes.read_episode(db, eid)
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
                if row["position"] <= floor:
                    continue
                cap = (
                    policy.source_bytes
                    if byte_limit is None
                    else min(policy.source_bytes, byte_limit)
                )
                source = self.episodes.read_source(db, row, max(0, cap - used))
                if source.turn is None:
                    raise HistoryError("Learning source incomplete or oversized.")
                used += source.payload_bytes
                records.append((row["conversation_id"], source))
            records.sort(key=lambda pair: (pair[1].started_at, pair[1].position))
            refs = {
                "run_floor": floor,
                "runs": [
                    {"id": s.run_id, "hash": s.turn.source_hash, "conversation_id": cid}
                    for cid, s in records
                ],
                "episodes": episode_refs,
            }
            return refs, records, hints

    def activate(self, now: datetime) -> None:
        """First enable is a watermark, never silently relearn pre-P2 history."""
        with self.store._transaction() as db:
            run_floor = db.execute(
                "SELECT COALESCE(MAX(r.rowid),0) FROM runs r JOIN conversations c "
                "ON c.id=r.conversation_id WHERE c.agent_id=?",
                (self.agent_id,),
            ).fetchone()[0]
            episode_floor = db.execute(
                "SELECT COALESCE(MAX(e.rowid),0) FROM episodes e JOIN conversations c "
                "ON c.id=e.stream_id WHERE c.agent_id=?",
                (self.agent_id,),
            ).fetchone()[0]
            db.execute(
                "INSERT OR IGNORE INTO memory_learning_state "
                "(agent_id,activated_at,run_floor,episode_floor,priority_cursor) "
                "VALUES (?,?,?,?,?)",
                (self.agent_id, now.isoformat(), run_floor, episode_floor, run_floor),
            )

    def activation(self) -> dict:
        with self.store._transaction() as db:
            row = db.execute(
                "SELECT * FROM memory_learning_state WHERE agent_id=?", (self.agent_id,)
            ).fetchone()
            if row is None:
                raise HistoryError("Learning is not activated.")
            return dict(row)

    def harvest_priority(self, now: datetime, policy: LearningPolicy) -> None:
        state = self.activation()
        with self.store._transaction() as db:
            rows = db.execute(
                "SELECT r.rowid AS position,r.id,r.status,r.conversation_id,"
                "(SELECT substr(json_extract(m.payload,'$.text'),1,2048) FROM messages m "
                "WHERE m.run_id=r.id AND json_extract(m.payload,'$.role')='user' "
                "ORDER BY m.sequence LIMIT 1) AS text "
                "FROM runs r JOIN conversations c ON c.id=r.conversation_id WHERE c.agent_id=? "
                "AND r.rowid>? ORDER BY r.rowid LIMIT 16",
                (self.agent_id, state["priority_cursor"]),
            ).fetchall()
        cursor = state["priority_cursor"]
        for row in rows:
            if row["status"] == "running":
                break
            if (
                row["status"] == "completed"
                and policy.priority_enabled
                and re.search(
                    r"记住|偏好|正式决定|明确更正|记错了|以后.{0,20}回答|\bremember\b|\bprefer\b|"
                    r"\bwe (?:have )?decided\b|\bcorrection\b",
                    row["text"] or "",
                    re.I,
                )
            ):
                try:
                    refs, _, _ = self.snapshot([row["id"]], [], policy)
                    self.create("priority", refs, now)
                except HistoryError:
                    refs = {
                        "run_floor": 0,
                        "runs": [
                            {"id": row["id"], "hash": "", "conversation_id": row["conversation_id"]}
                        ],
                        "episodes": [],
                    }
                    jid = self.create("priority", refs, now)
                    self.save(jid, "failed", now, error="source_unavailable_or_oversized")
            cursor = row["position"]
        with self.store._transaction() as db:
            db.execute(
                "UPDATE memory_learning_state SET priority_cursor=? WHERE agent_id=?",
                (cursor, self.agent_id),
            )

    def pending_job(self, kind: str, *, retry=False) -> dict | None:
        with self.store._transaction() as db:
            row = db.execute(
                "SELECT id FROM memory_learning_jobs WHERE agent_id=? AND kind=? "
                "AND state IN (" + ("'pending','failed'" if retry else "'pending'") + ") "
                "ORDER BY created_at,id LIMIT 1",
                (self.agent_id, kind),
            ).fetchone()
        return self.get(row[0]) if row else None

    def pending_episodes(self, limit: int) -> list[dict]:
        state = self.activation()
        with self.store._transaction() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT e.id,e.ended_at,e.revision FROM episodes e JOIN conversations c "
                    "ON c.id=e.stream_id WHERE c.agent_id=? AND e.rowid>? AND e.status='finalized' "
                    "AND NOT EXISTS (SELECT 1 FROM memory_learning_sources s WHERE s.agent_id=? "
                    "AND s.kind='episode' AND s.source_id=e.id AND s.revision=e.revision) "
                    "ORDER BY e.ended_at,e.id LIMIT ?",
                    (self.agent_id, state["episode_floor"], self.agent_id, limit),
                ).fetchall()
            ]

    def schedule_curator(self, now: datetime, interval: float, *, completed: bool = False) -> None:
        with self.store._transaction() as db:
            db.execute(
                "UPDATE memory_learning_state SET curator_due_at=?,"
                "curator_updates=CASE WHEN ? THEN updates ELSE curator_updates END "
                "WHERE agent_id=?",
                ((now + timedelta(seconds=interval)).isoformat(), completed, self.agent_id),
            )

    def latest_job(self, kind: str) -> dict | None:
        with self.store.reading() as db:
            row = db.execute(
                "SELECT id FROM memory_learning_jobs WHERE agent_id=? AND kind=? "
                "ORDER BY rowid DESC LIMIT 1",
                (self.agent_id, kind),
            ).fetchone()
        return self.get(row[0]) if row else None

    def recent_write_paths(self) -> list[str]:
        with self.store.reading() as db:
            rows = db.execute(
                "SELECT operations FROM memory_learning_jobs WHERE agent_id=? "
                "AND kind!='curator' AND state='completed' ORDER BY rowid DESC LIMIT 4",
                (self.agent_id,),
            ).fetchall()
        return [op["path"] for row in rows for op in json.loads(row[0])]

    def status(self) -> dict:
        with self.store._transaction() as db:
            counts = [
                dict(r)
                for r in db.execute(
                    "SELECT kind,state,COUNT(*) AS count FROM memory_learning_jobs "
                    "WHERE agent_id=? "
                    "GROUP BY kind,state",
                    (self.agent_id,),
                ).fetchall()
            ]
            latest = db.execute(
                "SELECT id,kind,state,updated_at,error,metrics FROM memory_learning_jobs "
                "WHERE agent_id=? ORDER BY rowid DESC LIMIT 1",
                (self.agent_id,),
            ).fetchone()
            attention = [
                dict(r)
                for r in db.execute(
                    "SELECT id,kind,state,error,"
                    "json_extract(metrics,'$.recovery.attempts') AS recovery_attempts,"
                    "json_extract(metrics,'$.recovery.next_check_at') AS recovery_next_check_at "
                    "FROM memory_learning_jobs WHERE agent_id=? "
                    "AND state IN ('failed','requires_reconciliation') ORDER BY rowid LIMIT 8",
                    (self.agent_id,),
                ).fetchall()
            ]
            success = db.execute(
                "SELECT updated_at FROM memory_learning_jobs WHERE agent_id=? "
                "AND state='completed' AND kind!='curator' "
                "ORDER BY rowid DESC LIMIT 1",
                (self.agent_id,),
            ).fetchone()
        last = dict(latest) if latest else None
        if last:
            last["metrics"] = json.loads(last["metrics"])
        return {
            "counts": counts,
            "last": last,
            "attention": attention,
            "last_learning_success_at": success[0] if success else None,
            "pending_episodes_capped": len(self.pending_episodes(65)),
            "activation": self.activation(),
        }

    def unresolved(self) -> bool:
        with self.store._transaction() as db:
            return bool(
                db.execute(
                    "SELECT 1 FROM memory_learning_jobs WHERE agent_id=? "
                    "AND state='requires_reconciliation' LIMIT 1",
                    (self.agent_id,),
                ).fetchone()
            )

    def related_sources(self, paths: list[str]) -> dict[str, list[dict]]:
        """Bounded job/receipt metadata lookup; Markdown and fact bodies are not searched."""
        if len(set(paths)) != len(paths) or len(paths) > 3:
            raise HistoryError("Curator target range exceeds budget.")
        result = {}
        for path in paths:
            with self.store._transaction() as db, query_budget(db):
                rows = db.execute(
                    "SELECT j.sources FROM memory_learning_jobs j WHERE j.agent_id=? "
                    "AND j.kind!='curator' AND j.state='completed' AND EXISTS ("
                    "SELECT 1 FROM json_each(j.operations) op "
                    "WHERE json_extract(op.value,'$.path')=? "
                    "AND json_extract(op.value,'$.state')='applied' "
                    "AND json_extract(op.value,'$.action') IN ('create','update')) "
                    "ORDER BY j.rowid DESC LIMIT 4",
                    (self.agent_id, path),
                ).fetchall()
            seen, references = set(), []
            for row in rows:
                for ref in reversed(json.loads(row["sources"])["runs"][:48]):
                    if ref["id"] not in seen:
                        references.append(ref)
                        seen.add(ref["id"])
            result[path] = references[:8]
        return result

    def running(self) -> bool:
        with self.store._transaction() as db:
            return (
                db.execute(
                    "SELECT 1 FROM runs r JOIN conversations c ON c.id=r.conversation_id "
                    "WHERE c.agent_id=? AND r.status='running' LIMIT 1",
                    (self.agent_id,),
                ).fetchone()
                is not None
            )

    def recovery_job(self, now: datetime, attempts: int) -> dict | None:
        with self.store._transaction() as db:
            row = db.execute(
                "SELECT id FROM memory_learning_jobs WHERE agent_id=? "
                "AND state='requires_reconciliation' "
                "AND COALESCE(json_extract(metrics,'$.recovery.attempts'),0)<? "
                "AND (json_extract(metrics,'$.recovery.next_check_at') IS NULL OR "
                "julianday(json_extract(metrics,'$.recovery.next_check_at'))<=julianday(?)) "
                "ORDER BY rowid LIMIT 1",
                (self.agent_id, attempts, now.isoformat()),
            ).fetchone()
        return self.get(row[0]) if row else None

    def create(self, kind: str, refs: dict, now: datetime) -> str:
        jid = uuid4().hex
        with self.store._transaction() as db:
            for source_kind, rows in (("run", refs["runs"]), ("episode", refs["episodes"])):
                if kind == "curator":
                    continue
                if kind == "batch" and source_kind == "run":
                    continue
                for row in rows:
                    found = db.execute(
                        "SELECT j.id,j.state,j.sources FROM memory_learning_sources s JOIN "
                        "memory_learning_jobs j ON j.id=s.job_id WHERE s.agent_id=? "
                        "AND s.kind=? AND s.source_id=? AND s.revision=?",
                        (self.agent_id, source_kind, row["id"], row.get("revision", 0)),
                    ).fetchone()
                    if found:
                        if json.loads(found["sources"]) != refs:
                            raise HistoryError(
                                "Overlapping learning sources require separate batches."
                            )
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
                if kind == "curator":
                    continue
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

    def save(
        self,
        jid: str,
        state: str,
        now: datetime,
        *,
        operations=None,
        error="",
        metrics=None,
        sources=None,
    ) -> None:
        old = self.get(jid)
        if metrics is not None:
            metrics = dict(metrics)
            # Recovery receipts/progress survive model metrics and publication checkpoints.
            if "recovery" not in metrics and "recovery" in old["metrics"]:
                metrics["recovery"] = old["metrics"]["recovery"]
        with self.store._transaction() as db:
            changed = db.execute(
                "UPDATE memory_learning_jobs SET state=?,updated_at=?,operations=?,"
                "error=?,metrics=?,sources=? "
                "WHERE id=? AND agent_id=? AND state=? AND updated_at=?",
                (
                    state,
                    now.isoformat(),
                    json.dumps(operations if operations is not None else old["operations"]),
                    error,
                    json.dumps(metrics if metrics is not None else old["metrics"]),
                    json.dumps(sources if sources is not None else old["sources"]),
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

    def recover(self, now: datetime) -> None:
        with self.store._transaction() as db:
            db.execute(
                "UPDATE memory_learning_jobs SET state=CASE WHEN operations='[]' "
                "THEN 'pending' ELSE 'requires_reconciliation' END,updated_at=? "
                "WHERE agent_id=? AND state='processing'",
                (now.isoformat(), self.agent_id),
            )
