"""Agent-bound incremental sources, CAS publication and shadow decision audit."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from personal_agent.core.evidence import redact
from personal_agent.core.history import HistoryError
from personal_agent.proactive.models import (
    AttentionDecision,
    ProactivePolicy,
    ScoutResult,
    UserSource,
)

if TYPE_CHECKING:
    from personal_agent.storage.sqlite import SQLiteStore


SCHEMA = (
    """CREATE TABLE proactive_state (
        agent_id TEXT PRIMARY KEY REFERENCES agents(id), baseline INTEGER NOT NULL,
        run_cursor INTEGER NOT NULL, thread_cursor INTEGER NOT NULL,
        error_code TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE proactive_proposals (
        id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id),
        topic_key TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'follow_up',
        title TEXT NOT NULL, context TEXT NOT NULL, sources TEXT NOT NULL,
        thread_id TEXT REFERENCES topic_threads(id), source_position INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN
            ('open','deferred','resolved','dismissed','expired')),
        revision INTEGER NOT NULL, next_review_at TEXT, expires_at TEXT NOT NULL,
        last_action TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
        UNIQUE(agent_id,topic_key))""",
    "CREATE INDEX proactive_due ON proactive_proposals(agent_id,status,next_review_at)",
    """CREATE TABLE proactive_decisions (
        id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id),
        created_at TEXT NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL,
        payload TEXT NOT NULL CHECK(length(CAST(payload AS BLOB))<=32768))""",
    "CREATE INDEX proactive_audit ON proactive_decisions(agent_id,created_at)",
)


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class ProactiveRepository:
    def __init__(self, store: SQLiteStore, agent_id: str) -> None:
        self.store, self.agent_id = store, agent_id

    def initialize(self) -> None:
        # No default historical catch-up. Existing runs and thread versions are excluded.
        with self.store._transaction() as db:
            last = db.execute(
                "SELECT COALESCE(MAX(r.rowid),0) FROM runs r JOIN conversations c "
                "ON c.id=r.conversation_id WHERE c.agent_id=?",
                (self.agent_id,),
            ).fetchone()[0]
            thread = db.execute(
                "SELECT COALESCE(MAX(v.rowid),0) FROM thread_versions v JOIN topic_threads t "
                "ON t.id=v.thread_id WHERE t.agent_id=?",
                (self.agent_id,),
            ).fetchone()[0]
            db.execute(
                "INSERT OR IGNORE INTO proactive_state VALUES (?,?,?,?, '')",
                (self.agent_id, last, last, thread),
            )

    def state(self) -> dict | None:
        with self.store.reading() as db:
            row = db.execute(
                "SELECT * FROM proactive_state WHERE agent_id=?", (self.agent_id,)
            ).fetchone()
            return dict(row) if row else None

    def activity(self) -> tuple[int, int, int]:
        with self.store.reading() as db:
            latest_message = db.execute(
                "SELECT m.rowid FROM messages m JOIN conversations c "
                "ON c.id=m.conversation_id WHERE c.agent_id=? ORDER BY m.rowid DESC LIMIT 1",
                (self.agent_id,),
            ).fetchone()
            latest_run = db.execute(
                "SELECT r.rowid FROM runs r JOIN conversations c ON c.id=r.conversation_id "
                "WHERE c.agent_id=? ORDER BY r.rowid DESC LIMIT 1",
                (self.agent_id,),
            ).fetchone()
            active = db.execute(
                "SELECT 1 FROM runs r JOIN conversations c ON c.id=r.conversation_id "
                "WHERE c.agent_id=? AND r.status='running' LIMIT 1",
                (self.agent_id,),
            ).fetchone()
            return (
                latest_message[0] if latest_message else 0,
                latest_run[0] if latest_run else 0,
                int(active is not None),
            )

    def pending(self) -> bool:
        state = self.state()
        if not state or state["error_code"]:
            return False
        with self.store.reading() as db:
            run = db.execute(
                "SELECT r.status FROM runs r JOIN conversations c ON c.id=r.conversation_id "
                "WHERE c.agent_id=? AND r.rowid>? ORDER BY r.rowid LIMIT 1",
                (self.agent_id, state["run_cursor"]),
            ).fetchone()
            if run:
                return run[0] != "running"
            return (
                db.execute(
                    "SELECT 1 FROM thread_versions v JOIN topic_threads t ON t.id=v.thread_id "
                    "WHERE t.agent_id=? AND v.rowid>? LIMIT 1",
                    (self.agent_id, state["thread_cursor"]),
                ).fetchone()
                is not None
            )

    def _users(self, db, rows, policy: ProactivePolicy) -> tuple[list[UserSource], str]:
        users, used = [], 0
        for row in rows:
            meta = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(length(CAST("
                "json_extract(payload,'$.text') AS BLOB))),0) "
                "FROM messages WHERE run_id=? AND json_extract(payload,'$.role')='user' "
                "AND json_type(payload,'$.text')='text'",
                (row["id"],),
            ).fetchone()
            if len(users) + meta[0] > 64 or used + meta[1] > policy.source_bytes:
                return [], "source_budget"
            used += meta[1]
            for message in db.execute(
                "SELECT sequence,json_extract(payload,'$.text') AS text FROM messages "
                "WHERE run_id=? AND json_extract(payload,'$.role')='user' "
                "AND json_type(payload,'$.text')='text' ORDER BY sequence",
                (row["id"],),
            ):
                users.append(
                    UserSource(
                        conversation_id=row["conversation_id"],
                        run_id=row["id"],
                        sequence=message["sequence"],
                        text=redact(message["text"]),
                        position=row["position"],
                        ended_at=row["ended_at"],
                    )
                )
        return users, ""

    def snapshot(self, policy: ProactivePolicy) -> dict | None:
        state = self.state()
        if not state or state["error_code"]:
            return None
        with self.store.reading() as db:
            row = db.execute(
                "SELECT r.rowid AS position,r.* FROM runs r JOIN conversations c "
                "ON c.id=r.conversation_id WHERE c.agent_id=? AND r.rowid>? "
                "ORDER BY r.rowid LIMIT 1",
                (self.agent_id, state["run_cursor"]),
            ).fetchone()
            if row:
                if row["status"] == "running":
                    return None
                users, skip = (
                    self._users(db, [row], policy)
                    if row["status"] == "completed"
                    else ([], "run_not_completed")
                )
                return dict(
                    state=state,
                    lane="run_cursor",
                    position=row["position"],
                    users=users,
                    skip=skip,
                    thread_id=None,
                    hint=None,
                )
            version = db.execute(
                "SELECT v.rowid AS position,v.*,t.revision AS latest,t.dirty,t.source_revision "
                "FROM thread_versions v "
                "JOIN topic_threads t ON t.id=v.thread_id WHERE t.agent_id=? AND v.rowid>? "
                "ORDER BY v.rowid LIMIT 1",
                (self.agent_id, state["thread_cursor"]),
            ).fetchone()
            if not version:
                return None
            # Thread text is a hint, never evidence. Only post-enable user sources qualify.
            rows = db.execute(
                "SELECT r.rowid AS position,r.* FROM runs r JOIN conversations c "
                "ON c.id=r.conversation_id JOIN episode_runs er ON er.run_id=r.id "
                "JOIN episodes e ON e.id=er.episode_id WHERE c.agent_id=? "
                "AND e.thread_id=? AND r.rowid>? AND r.status='completed' "
                "ORDER BY r.rowid DESC LIMIT 5",
                (self.agent_id, version["thread_id"], state["baseline"]),
            ).fetchall()
            stale = version["revision"] != version["latest"] or bool(version["dirty"])
            users, skip = ([], "stale_thread") if stale else self._users(db, rows[:4], policy)
            if len(rows) > 4:
                users, skip = [], "thread_source_budget"
            return dict(
                state=state,
                lane="thread_cursor",
                position=version["position"],
                users=users,
                skip=skip,
                thread_id=version["thread_id"],
                hint=json.loads(version["payload"]) if not stale else None,
                thread_revision=version["revision"],
                thread_source_revision=version["source_revision"],
            )

    def candidates(self) -> list[dict]:
        with self.store.reading() as db:
            rows = db.execute(
                "SELECT * FROM proactive_proposals WHERE agent_id=? AND status IN "
                "('open','deferred') ORDER BY source_position DESC LIMIT 48",
                (self.agent_id,),
            ).fetchall()
            closed = db.execute(
                "SELECT * FROM proactive_proposals WHERE agent_id=? AND status NOT IN "
                "('open','deferred') ORDER BY source_position DESC LIMIT 16",
                (self.agent_id,),
            ).fetchall()
            return [self._decode(row) for row in [*rows, *closed]]

    @staticmethod
    def _decode(row) -> dict:
        value = dict(row)
        value["sources"] = json.loads(value["sources"])
        return value

    def _event(self, db, at: datetime, action: str, reason: str, payload: dict) -> None:
        db.execute(
            "INSERT INTO proactive_decisions VALUES (?,?,?,?,?,?)",
            (
                str(uuid4()),
                self.agent_id,
                stamp(at),
                action,
                reason,
                json.dumps(payload, ensure_ascii=False),
            ),
        )

    def publish(
        self, snapshot: dict, result: ScoutResult, policy: ProactivePolicy, at: datetime
    ) -> None:
        if snapshot["lane"] == "run_cursor" and snapshot["skip"] == "source_budget":
            raise HistoryError("Unreviewed user source exceeds budget.")
        users = {(s.conversation_id, s.run_id, s.sequence): s for s in snapshot["users"]}
        with self.store._transaction() as db:
            current = db.execute(
                "SELECT * FROM proactive_state WHERE agent_id=?", (self.agent_id,)
            ).fetchone()
            if dict(current) != snapshot["state"]:
                raise HistoryError("Proactive source progress changed.")
            if snapshot["thread_id"]:
                version = db.execute(
                    "SELECT revision,source_revision,dirty FROM topic_threads "
                    "WHERE id=? AND agent_id=?",
                    (snapshot["thread_id"], self.agent_id),
                ).fetchone()
                if not version or (
                    not snapshot["skip"]
                    and (
                        version[0] != snapshot["thread_revision"]
                        or version[1] != snapshot["thread_source_revision"]
                        or version[2]
                    )
                ):
                    raise HistoryError("Proactive thread changed.")
            for change in result.changes:
                position = max(
                    users[(r.conversation_id, r.run_id, r.sequence)].position
                    for r in change.sources
                )
                previous = db.execute(
                    "SELECT * FROM proactive_proposals WHERE agent_id=? AND topic_key=?",
                    (self.agent_id, change.topic_key),
                ).fetchone()
                # Old summaries cannot reopen a cancelled/resolved opportunity.
                if previous and position <= previous["source_position"]:
                    if snapshot["thread_id"] and previous["thread_id"] is None:
                        db.execute(
                            "UPDATE proactive_proposals SET thread_id=? WHERE id=?",
                            (snapshot["thread_id"], previous["id"]),
                        )
                        self._event(
                            db,
                            at,
                            "linked_thread",
                            "metadata_only",
                            dict(
                                proposal_id=previous["id"],
                                thread_id=snapshot["thread_id"],
                            ),
                        )
                    continue
                count = db.execute(
                    "SELECT COUNT(*) FROM proactive_proposals WHERE agent_id=? "
                    "AND status IN ('open','deferred')",
                    (self.agent_id,),
                ).fetchone()[0]
                if (
                    change.status == "open"
                    and count >= 48
                    and (not previous or previous["status"] not in {"open", "deferred"})
                ):
                    self._event(
                        db,
                        at,
                        "skipped",
                        "active_capacity",
                        dict(topic_key=change.topic_key, position=position),
                    )
                    continue
                source_at = max(
                    users[(r.conversation_id, r.run_id, r.sequence)].ended_at
                    for r in change.sources
                )
                expiry = source_at + timedelta(seconds=policy.lifetime_seconds)
                status = "expired" if change.status == "open" and expiry <= at else change.status
                review = max(
                    at,
                    change.next_review_at or (source_at + timedelta(seconds=policy.review_seconds)),
                )
                raw = json.dumps(
                    [r.model_dump(mode="json") for r in change.sources], ensure_ascii=False
                )
                db.execute(
                    "INSERT INTO proactive_proposals (id,agent_id,topic_key,title,context,sources,"
                    "thread_id,source_position,status,revision,next_review_at,expires_at,"
                    "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(agent_id,topic_key) DO UPDATE SET "
                    "title=excluded.title,context=excluded.context,sources=excluded.sources,"
                    "thread_id=COALESCE(excluded.thread_id,proactive_proposals.thread_id),"
                    "source_position=excluded.source_position,status=excluded.status,"
                    "revision=proactive_proposals.revision+1,next_review_at=excluded.next_review_at,"
                    "expires_at=excluded.expires_at,last_action='',updated_at=excluded.updated_at",
                    (
                        previous["id"] if previous else str(uuid4()),
                        self.agent_id,
                        change.topic_key,
                        change.title,
                        change.context,
                        raw,
                        snapshot["thread_id"],
                        position,
                        status,
                        1,
                        stamp(min(review, expiry)) if status == "open" else None,
                        stamp(expiry),
                        stamp(at),
                    ),
                )
            lane = snapshot["lane"]
            if lane not in {"run_cursor", "thread_cursor"}:
                raise ValueError("Invalid cursor lane")
            db.execute(
                f"UPDATE proactive_state SET {lane}=? WHERE agent_id=?",
                (snapshot["position"], self.agent_id),
            )
            self._event(
                db,
                at,
                "scouted",
                snapshot["skip"] or "source_processed",
                dict(lane=lane, position=snapshot["position"], changes=len(result.changes)),
            )

    def fail(self, code: str, at: datetime) -> None:
        with self.store._transaction() as db:
            db.execute(
                "UPDATE proactive_state SET error_code=? WHERE agent_id=?", (code, self.agent_id)
            )
            self._event(db, at, "failed", code, {})

    def retry(self, at: datetime) -> None:
        with self.store._transaction() as db:
            db.execute(
                "UPDATE proactive_state SET error_code='' WHERE agent_id=?", (self.agent_id,)
            )
            db.execute(
                "UPDATE proactive_proposals SET last_action='',next_review_at=? "
                "WHERE agent_id=? AND last_action='failed' AND status IN ('open','deferred')",
                (stamp(at), self.agent_id),
            )

    def expire(self, at: datetime) -> int:
        with self.store._transaction() as db:
            rows = db.execute(
                "SELECT id FROM proactive_proposals WHERE agent_id=? AND status IN "
                "('open','deferred') AND expires_at<=? LIMIT 48",
                (self.agent_id, stamp(at)),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE proactive_proposals SET status='expired',revision=revision+1,"
                    "next_review_at=NULL,updated_at=? WHERE id=?",
                    (stamp(at), row["id"]),
                )
            if rows:
                self._event(db, at, "expired", "lifetime_ended", dict(count=len(rows)))
            return len(rows)

    def has_expired(self, at: datetime) -> bool:
        with self.store.reading() as db:
            return (
                db.execute(
                    "SELECT 1 FROM proactive_proposals WHERE agent_id=? "
                    "AND status IN ('open','deferred') AND expires_at<=? LIMIT 1",
                    (self.agent_id, stamp(at)),
                ).fetchone()
                is not None
            )

    def due(self, at: datetime, limit: int) -> list[dict]:
        if not 1 <= limit <= 4:
            raise ValueError("Invalid candidate limit")
        with self.store.reading() as db:
            return [
                self._decode(r)
                for r in db.execute(
                    "SELECT * FROM proactive_proposals WHERE agent_id=? AND status IN "
                    "('open','deferred') AND next_review_at<=? AND expires_at>? "
                    "ORDER BY next_review_at,id LIMIT ?",
                    (self.agent_id, stamp(at), stamp(at), limit),
                )
            ]

    def decide(
        self,
        proposals: list[dict],
        decision: AttentionDecision,
        activity: tuple,
        at: datetime,
        policy: ProactivePolicy,
        *,
        failed: bool = False,
    ) -> bool:
        # No awaits between final activity/CAS check and publication.
        if self.activity() != activity or self.pending():
            return False
        with self.store._transaction() as db:
            for proposal in proposals:
                row = db.execute(
                    "SELECT revision,status,expires_at FROM proactive_proposals "
                    "WHERE id=? AND agent_id=?",
                    (proposal["id"], self.agent_id),
                ).fetchone()
                if (
                    not row
                    or row["revision"] != proposal["revision"]
                    or row["status"] not in {"open", "deferred"}
                    or row["expires_at"] <= stamp(at)
                ):
                    return False
            action = "failed" if failed else decision.action
            for proposal in proposals:
                if proposal["id"] not in decision.proposal_ids:
                    continue
                # Unchanged notify/silent is not reconsidered until new evidence or expiry.
                review = None
                if decision.action == "defer" and not failed:
                    review = stamp(
                        min(
                            max(
                                decision.next_review_at or at,
                                at + timedelta(seconds=policy.review_seconds),
                            ),
                            datetime.fromisoformat(proposal["expires_at"]),
                        )
                    )
                db.execute(
                    "UPDATE proactive_proposals SET status=?,next_review_at=?,last_action=?,"
                    "revision=revision+1,updated_at=? WHERE id=? AND agent_id=?",
                    (
                        "deferred" if decision.action == "defer" else "open",
                        review,
                        action,
                        stamp(at),
                        proposal["id"],
                        self.agent_id,
                    ),
                )
            self._event(
                db,
                at,
                action,
                decision.reason,
                dict(
                    mode="shadow",
                    decision=decision.model_dump(mode="json"),
                    revisions={p["id"]: p["revision"] for p in proposals},
                ),
            )
            return True

    def listing(self, kind: str, limit: int = 12, cursor: int = 2**63 - 1) -> dict:
        if kind not in {"proposals", "decisions"} or not 1 <= limit <= 20 or cursor < 1:
            raise ValueError("Invalid proactive page")
        table = "proactive_" + kind
        with self.store.reading() as db:
            rows = db.execute(
                f"SELECT rowid AS position,* FROM {table} WHERE agent_id=? "
                "AND rowid<? ORDER BY rowid DESC LIMIT ?",
                (self.agent_id, cursor, limit + 1),
            ).fetchall()
        items = [dict(r) for r in rows[:limit]]
        next_cursor = items[-1]["position"] if len(rows) > limit else None
        for item in items:
            item.pop("position")
            key = "sources" if kind == "proposals" else "payload"
            item[key] = json.loads(item[key])
        return dict(items=items, next_cursor=next_cursor, has_more=next_cursor is not None)
