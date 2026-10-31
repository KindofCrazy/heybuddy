"""Bounded drafts and attempts, with CAS claims and no uncertain delivery replay."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from personal_agent.core.evidence import SECRET
from personal_agent.core.history import HistoryError
from personal_agent.proactive.delivery import DeliveryGrant
from personal_agent.storage.proactive import ProactiveRepository, stamp

if TYPE_CHECKING:
    from personal_agent.storage.sqlite import SQLiteStore


SCHEMA = (
    """CREATE TABLE proactive_outbox (
        id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id),
        decision_id TEXT NOT NULL REFERENCES proactive_decisions(id),
        proposal_id TEXT NOT NULL REFERENCES proactive_proposals(id),
        proposal_revision INTEGER NOT NULL, conversation_id TEXT NOT NULL,
        activity TEXT NOT NULL, receipt_epoch INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN
          ('composing','prepared','compose_failed','sending','accepted','failed','uncertain','cancelled')),
        mode TEXT NOT NULL CHECK(mode IN ('preview','simulation')),
        text TEXT NOT NULL DEFAULT '' CHECK(length(CAST(text AS BLOB))<=4800),
        persona_fingerprint TEXT NOT NULL, recipient TEXT, grant_revision INTEGER,
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        error_code TEXT NOT NULL DEFAULT '', receipt_id TEXT,
        UNIQUE(agent_id,decision_id),
        FOREIGN KEY(conversation_id,agent_id) REFERENCES conversations(id,agent_id))""",
    "CREATE INDEX outbox_agent_state ON proactive_outbox(agent_id,status)",
    """CREATE TABLE proactive_attempts (
        id TEXT PRIMARY KEY REFERENCES proactive_outbox(id),
        agent_id TEXT NOT NULL REFERENCES agents(id), recipient TEXT NOT NULL,
        started_at TEXT NOT NULL, ended_at TEXT,
        status TEXT NOT NULL CHECK(status IN ('sending','accepted','failed','uncertain')),
        error_code TEXT NOT NULL DEFAULT '', receipt_id TEXT)""",
    "CREATE INDEX attempts_recipient ON proactive_attempts(agent_id,recipient,started_at)",
)


class OutboxRepository:
    def __init__(self, store: SQLiteStore, agent_id: str) -> None:
        self.store, self.agent_id = store, agent_id
        self.proactive = ProactiveRepository(store, agent_id)

    def recover(self, at: datetime) -> None:
        with self.store._transaction() as db:
            db.execute(
                "UPDATE proactive_outbox SET status='compose_failed',"
                "error_code='compose_interrupted',updated_at=? "
                "WHERE agent_id=? AND status='composing'",
                (stamp(at), self.agent_id),
            )
            db.execute(
                "UPDATE proactive_outbox SET status='cancelled',"
                "error_code='restart_requires_recheck',updated_at=? "
                "WHERE agent_id=? AND status='prepared'",
                (stamp(at), self.agent_id),
            )
            db.execute(
                "UPDATE proactive_outbox SET status='uncertain',"
                "error_code='send_interrupted',updated_at=? "
                "WHERE agent_id=? AND status='sending'",
                (stamp(at), self.agent_id),
            )
            db.execute(
                "UPDATE proactive_attempts SET status='uncertain',"
                "error_code='send_interrupted',ended_at=? "
                "WHERE agent_id=? AND status='sending'",
                (stamp(at), self.agent_id),
            )

    def _get(self, db, item_id: str):
        row = db.execute(
            "SELECT * FROM proactive_outbox WHERE id=? AND agent_id=?", (item_id, self.agent_id)
        ).fetchone()
        if not row:
            raise HistoryError("Outbox item is unavailable.")
        return dict(row)

    def get(self, item_id: str) -> dict:
        with self.store.reading() as db:
            return self._get(db, item_id)

    def next_decision(self) -> dict | None:
        with self.store.reading() as db:
            row = db.execute(
                "SELECT d.* FROM proactive_decisions d WHERE d.agent_id=? AND d.action='notify' "
                "AND NOT EXISTS (SELECT 1 FROM proactive_outbox o "
                "WHERE o.agent_id=d.agent_id AND o.decision_id=d.id) ORDER BY d.rowid LIMIT 1",
                (self.agent_id,),
            ).fetchone()
            return dict(row) if row else None

    def proposal(self, proposal_id: str, revision: int, at: datetime) -> dict | None:
        with self.store.reading() as db:
            return self._proposal(db, proposal_id, revision, at)

    def _proposal(self, db, proposal_id: str, revision: int, at: datetime) -> dict | None:
        row = db.execute(
            "SELECT * FROM proactive_proposals WHERE id=? AND agent_id=?",
            (proposal_id, self.agent_id),
        ).fetchone()
        if (
            not row
            or row["revision"] != revision
            or row["last_action"] != "notify"
            or row["status"] not in {"open", "deferred"}
            or row["expires_at"] <= stamp(at)
        ):
            return None
        proposal = dict(row)
        proposal["sources"] = json.loads(proposal["sources"])
        proposal["followup_window"] = json.loads(proposal["followup_window"])
        for source in proposal["sources"]:
            raw = db.execute(
                "SELECT m.payload,length(CAST(m.payload AS BLOB)) AS size FROM messages m "
                "JOIN runs r ON r.id=m.run_id JOIN conversations c ON c.id=m.conversation_id "
                "WHERE c.agent_id=? AND m.conversation_id=? AND m.run_id=? AND m.sequence=? "
                "AND r.status='completed' AND length(CAST(m.payload AS BLOB))<=65536",
                (self.agent_id, source["conversation_id"], source["run_id"], source["sequence"]),
            ).fetchone()
            if not raw:
                return None
            message = json.loads(raw["payload"])
            if (
                message.get("role") != "user"
                or source["quote"] not in message.get("text", "")
                or SECRET.search(source["quote"])
            ):
                return None
        return proposal

    def begin(
        self,
        decision: dict,
        conversation_id: str,
        activity: tuple,
        epoch: int,
        fingerprint: str,
        at: datetime,
    ) -> dict:
        payload = json.loads(decision["payload"])
        selected = payload["decision"]["proposal_ids"]
        if len(selected) != 1:
            raise HistoryError("Invalid draft decision.")
        proposal_id = selected[0]
        revision = payload["revisions"][proposal_id] + 1
        with self.store._transaction() as db:
            canonical = db.execute(
                "SELECT * FROM proactive_decisions WHERE id=? AND agent_id=?",
                (decision["id"], self.agent_id),
            ).fetchone()
            if not canonical or dict(canonical) != decision or decision["action"] != "notify":
                raise HistoryError("Draft decision changed.")
            proposal = self._proposal(db, proposal_id, revision, at)
            valid = proposal is not None
            expiry = (
                min(datetime.fromisoformat(proposal["expires_at"]), at + timedelta(days=1))
                if proposal
                else at
            )
            item_id = str(uuid4())
            db.execute(
                "INSERT INTO proactive_outbox "
                "(id,agent_id,decision_id,proposal_id,proposal_revision,conversation_id,activity,"
                "receipt_epoch,status,mode,persona_fingerprint,created_at,expires_at,updated_at,"
                "error_code) VALUES (?,?,?,?,?,?,?,?,?,'preview',?,?,?,?,?)",
                (
                    item_id,
                    self.agent_id,
                    decision["id"],
                    proposal_id,
                    revision,
                    conversation_id,
                    json.dumps(activity),
                    epoch,
                    "composing" if valid else "cancelled",
                    fingerprint,
                    stamp(at),
                    stamp(expiry),
                    stamp(at),
                    "" if valid else "proposal_stale",
                ),
            )
            return self._get(db, item_id)

    def valid(
        self,
        item: dict,
        at: datetime,
        activity: tuple,
        epoch: int,
        conversation_id: str,
        fingerprint: str,
    ) -> bool:
        return bool(
            item["conversation_id"] == conversation_id
            and item["receipt_epoch"] == epoch
            and json.loads(item["activity"]) == list(activity)
            and item["persona_fingerprint"] == fingerprint
            and item["expires_at"] > stamp(at)
            and self.proposal(item["proposal_id"], item["proposal_revision"], at)
        )

    def restart(
        self,
        item_id: str,
        conversation_id: str,
        activity: tuple,
        epoch: int,
        fingerprint: str,
        at: datetime,
    ) -> dict:
        with self.store._transaction() as db:
            item = self._get(db, item_id)
            if (
                item["status"] != "compose_failed"
                or item["conversation_id"] != conversation_id
                or item["expires_at"] <= stamp(at)
                or self._proposal(db, item["proposal_id"], item["proposal_revision"], at) is None
            ):
                raise HistoryError("Draft cannot be regenerated from this source.")
            db.execute(
                "UPDATE proactive_outbox SET status='composing',text='',error_code='',"
                "activity=?,receipt_epoch=?,persona_fingerprint=?,updated_at=? "
                "WHERE id=? AND agent_id=?",
                (json.dumps(activity), epoch, fingerprint, stamp(at), item_id, self.agent_id),
            )
            return self._get(db, item_id)

    def prepare(self, item: dict, text: str, at: datetime) -> bool:
        if not text.strip() or len(text) > 1200 or SECRET.search(text):
            raise HistoryError("Draft content is invalid.")
        with self.store._transaction() as db:
            current = self._get(db, item["id"])
            if current["status"] != "composing" or current != item:
                return False
            if self._proposal(db, item["proposal_id"], item["proposal_revision"], at) is None:
                return False
            db.execute(
                "UPDATE proactive_outbox SET status='prepared',text=?,updated_at=? "
                "WHERE id=? AND agent_id=?",
                (text, stamp(at), item["id"], self.agent_id),
            )
            return True

    def stop(self, item_id: str, status: str, code: str, at: datetime) -> None:
        if status not in {"compose_failed", "cancelled"}:
            raise ValueError("Invalid draft transition")
        with self.store._transaction() as db:
            self._get(db, item_id)
            db.execute(
                "UPDATE proactive_outbox SET status=?,error_code=?,updated_at=? "
                "WHERE id=? AND agent_id=? AND status IN ('composing','prepared')",
                (status, code, stamp(at), item_id, self.agent_id),
            )

    def claim(self, item: dict, grant: DeliveryGrant, at: datetime) -> bool:
        with self.store._transaction() as db:
            current = self._get(db, item["id"])
            if (
                current != item
                or current["status"] != "prepared"
                or not grant.enabled
                or grant.conversation_id != current["conversation_id"]
                or self._proposal(db, item["proposal_id"], item["proposal_revision"], at) is None
            ):
                return False
            recent = db.execute(
                "SELECT COUNT(*),MAX(started_at) FROM proactive_attempts "
                "WHERE agent_id=? AND recipient=? AND started_at>?",
                (self.agent_id, grant.recipient, stamp(at - timedelta(days=1))),
            ).fetchone()
            if recent[0] >= grant.daily_limit or (
                recent[1]
                and datetime.fromisoformat(recent[1]) + timedelta(seconds=grant.cooldown_seconds)
                > at
            ):
                return False
            db.execute(
                "UPDATE proactive_outbox SET status='sending',mode='simulation',"
                "recipient=?,grant_revision=?,updated_at=? WHERE id=? AND agent_id=?",
                (grant.recipient, grant.revision, stamp(at), item["id"], self.agent_id),
            )
            db.execute(
                "INSERT INTO proactive_attempts (id,agent_id,recipient,started_at,status) "
                "VALUES (?,?,?,?,'sending')",
                (item["id"], self.agent_id, grant.recipient, stamp(at)),
            )
            return True

    def finish(
        self, item_id: str, status: str, at: datetime, *, code: str = "", receipt: str | None = None
    ) -> None:
        if status not in {"accepted", "failed", "uncertain"}:
            raise ValueError("Invalid delivery outcome")
        with self.store._transaction() as db:
            current = self._get(db, item_id)
            if current["status"] != "sending":
                raise HistoryError("Delivery attempt is not sending.")
            db.execute(
                "UPDATE proactive_outbox SET status=?,updated_at=?,error_code=?,receipt_id=? "
                "WHERE id=? AND agent_id=?",
                (status, stamp(at), code, receipt, item_id, self.agent_id),
            )
            db.execute(
                "UPDATE proactive_attempts SET status=?,ended_at=?,error_code=?,receipt_id=? "
                "WHERE id=? AND agent_id=?",
                (status, stamp(at), code, receipt, item_id, self.agent_id),
            )

    def listing(self, kind: str = "outbox", limit: int = 12, cursor: int = 2**63 - 1) -> dict:
        if kind not in {"outbox", "attempts"} or not 1 <= limit <= 20 or cursor < 1:
            raise ValueError("Invalid outbox page")
        with self.store.reading() as db:
            rows = db.execute(
                f"SELECT rowid AS position,* FROM proactive_{kind} "
                "WHERE agent_id=? AND rowid<? ORDER BY rowid DESC LIMIT ?",
                (self.agent_id, cursor, limit + 1),
            ).fetchall()
        items = [dict(r) for r in rows[:limit]]
        next_cursor = items[-1]["position"] if len(rows) > limit else None
        for item in items:
            item.pop("position")
            item["origin"] = "proactive"
        return dict(items=items, next_cursor=next_cursor, has_more=next_cursor is not None)


class OutboundContext:
    """Read-only bounded assistant notices; simulations require an explicit harness opt-in."""

    def __init__(
        self,
        repository: OutboxRepository,
        conversation_id: str,
        *,
        include_simulations: bool = False,
    ) -> None:
        self.repository, self.conversation_id = repository, conversation_id
        self.include_simulations = include_simulations

    def context(self) -> dict | None:
        if not self.include_simulations:
            # No real transport exists in this phase. Never imply a fake was delivered.
            return None
        with self.repository.store.reading() as db:
            rows = db.execute(
                "SELECT id,text,updated_at,mode FROM proactive_outbox WHERE agent_id=? "
                "AND conversation_id=? AND status='accepted' AND mode='simulation' "
                "ORDER BY rowid DESC LIMIT 3",
                (self.repository.agent_id, self.conversation_id),
            ).fetchall()
        if not rows:
            return None
        return dict(
            kind="proactive_messages",
            origin="proactive",
            trust="assistant_generated_only",
            user_evidence=False,
            simulation=True,
            messages=[dict(role="assistant", **dict(r)) for r in reversed(rows)],
        )
