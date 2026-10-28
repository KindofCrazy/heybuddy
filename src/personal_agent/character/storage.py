"""Agent/character-bound transient state and source receipts; no persona or relationship copy."""

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError

from personal_agent.character.models import CharacterState, StatePolicy
from personal_agent.core.history import HistoryError

if TYPE_CHECKING:
    from personal_agent.storage.episodes import EpisodeRepository

SCHEMA = (
    """CREATE TABLE character_runs (
       run_id TEXT PRIMARY KEY REFERENCES runs(id), agent_id TEXT NOT NULL REFERENCES agents(id),
       character_id TEXT NOT NULL, package_hash TEXT NOT NULL)""",
    "CREATE INDEX character_run_scope ON character_runs(agent_id,character_id,package_hash)",
    """CREATE TRIGGER character_run_agent BEFORE INSERT ON character_runs WHEN NOT EXISTS (
       SELECT 1 FROM runs r JOIN conversations c ON c.id=r.conversation_id
       WHERE r.id=NEW.run_id AND c.agent_id=NEW.agent_id)
       BEGIN SELECT RAISE(ABORT,'Character Run scope mismatch'); END""",
    """CREATE TABLE character_states (
       agent_id TEXT NOT NULL REFERENCES agents(id), character_id TEXT NOT NULL,
       package_hash TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
       source_position INTEGER NOT NULL DEFAULT 0, source_at TEXT,
       last_attempt_at TEXT, payload TEXT CHECK(length(CAST(payload AS BLOB))<=4096),
       PRIMARY KEY(agent_id,character_id))""",
    """CREATE TABLE character_state_sources (
       agent_id TEXT NOT NULL REFERENCES agents(id), character_id TEXT NOT NULL,
       package_hash TEXT NOT NULL, episode_id TEXT NOT NULL REFERENCES episodes(id),
       episode_revision INTEGER NOT NULL, source_hash TEXT NOT NULL,
       state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', metrics TEXT NOT NULL DEFAULT '{}',
       PRIMARY KEY(agent_id,character_id,package_hash,episode_id,episode_revision))""",
    "CREATE INDEX character_source_scope ON character_state_sources(agent_id,character_id,state)",
)


class CharacterStateRepository:
    def __init__(self, episodes: "EpisodeRepository", character_id: str, package_hash: str) -> None:
        self.episodes = episodes
        self.store = episodes.store
        self.agent_id = episodes.agent_id
        self.character_id = character_id
        self.package_hash = package_hash
        self.scope = (self.agent_id, character_id, package_hash)

    def initialize(self) -> None:
        with self.store._transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO character_states(agent_id,character_id,package_hash) "
                "VALUES (?,?,?)",
                self.scope,
            )
            db.execute(
                "UPDATE character_states SET package_hash=?,revision=revision+1,"
                "payload=NULL,source_position=0,source_at=NULL,last_attempt_at=NULL "
                "WHERE agent_id=? AND character_id=? AND package_hash!=?",
                (self.package_hash, self.agent_id, self.character_id, self.package_hash),
            )
            db.execute(
                "UPDATE character_state_sources SET state='pending',error='' WHERE "
                "agent_id=? AND character_id=? AND package_hash=? AND state='processing'",
                self.scope,
            )

    def tag_run(self, run_id: str) -> None:
        with self.store._transaction() as db:
            row = db.execute(
                "SELECT 1 FROM runs r JOIN conversations c ON c.id=r.conversation_id "
                "WHERE r.id=? AND c.agent_id=? AND r.status='running'",
                (run_id, self.agent_id),
            ).fetchone()
            if row is None:
                raise HistoryError("Character Run is not active in this Agent.")
            db.execute("INSERT INTO character_runs VALUES (?,?,?,?)", (run_id, *self.scope))

    def row(self) -> dict:
        with self.store.reading() as db:
            row = db.execute(
                "SELECT * FROM character_states WHERE agent_id=? AND character_id=? "
                "AND package_hash=?",
                self.scope,
            ).fetchone()
            if row is None:
                raise HistoryError("Character state is unavailable.")
            return dict(row)

    def references(self, run_ids: tuple[str, ...]) -> dict:
        """Bounded Host attribution only; no contents or cross-Agent labels are exposed."""
        ids = tuple(dict.fromkeys(run_ids))[:32]
        if not ids:
            return {}
        with self.store._transaction() as db:
            rows = db.execute(
                "SELECT r.id,t.character_id FROM runs r "
                "JOIN conversations c ON c.id=r.conversation_id "
                "LEFT JOIN character_runs t ON t.run_id=r.id AND t.agent_id=c.agent_id "
                "WHERE c.agent_id=? AND r.id IN (" + ",".join("?" for _ in ids) + ")",
                (self.agent_id, *ids),
            ).fetchall()
        return {
            row["id"]: (
                "unattributed"
                if row["character_id"] is None
                else "same_character"
                if row["character_id"] == self.character_id
                else "other_character"
            )
            for row in rows
        }

    def current(self) -> CharacterState | None:
        row = self.row()
        if row["payload"] is None:
            return None
        try:
            state = CharacterState.model_validate_json(row["payload"])
        except ValidationError:
            raise HistoryError("Character state is invalid.") from None
        if (
            state.character_id != self.character_id
            or state.revision != row["revision"]
            or state.observed_at.isoformat() != row["source_at"]
        ):
            raise HistoryError("Character state metadata differs.")
        return state

    def candidate(self, *, retry=False) -> dict | None:
        with self.store._transaction() as db:
            row = db.execute(
                "SELECT e.id,e.revision,s.state FROM episodes e JOIN conversations c "
                "ON c.id=e.stream_id LEFT JOIN character_state_sources s ON s.episode_id=e.id "
                "AND s.episode_revision=e.revision AND s.agent_id=? AND s.character_id=? "
                "AND s.package_hash=? WHERE c.agent_id=? AND "
                "(s.state IS NULL OR s.state='pending' "
                + ("OR s.state='failed'" if retry else "")
                + ") "
                "AND EXISTS (SELECT 1 FROM episode_runs er JOIN character_runs cr "
                "ON cr.run_id=er.run_id WHERE er.episode_id=e.id AND cr.agent_id=? "
                "AND cr.character_id=? AND cr.package_hash=?) "
                "ORDER BY e.ended_at DESC,e.id LIMIT 1",
                (*self.scope, self.agent_id, *self.scope),
            ).fetchone()
            return dict(row) if row else None

    def snapshot(self, episode_id: str, policy: StatePolicy) -> dict:
        with self.store._transaction() as db:
            episode = self.episodes._episode(db, episode_id)
            rows = db.execute(
                "SELECT r.rowid AS position,r.* FROM episode_runs er JOIN runs r "
                "ON r.id=er.run_id AND r.conversation_id=er.stream_id JOIN character_runs cr "
                "ON cr.run_id=r.id WHERE er.episode_id=? AND cr.agent_id=? "
                "AND cr.character_id=? AND cr.package_hash=? ORDER BY er.ordinal LIMIT 17",
                (episode.id, *self.scope),
            ).fetchall()
            if not rows or len(rows) > 16:
                raise HistoryError("Character source range exceeds its bound.")
            records = []
            used = 0
            for row in rows:
                source = self.episodes._source(db, row, policy.source_bytes - used)
                if source.turn is None:
                    raise HistoryError("Character source is incomplete or oversized.")
                used += source.payload_bytes
                records.append(source)
            signature = hashlib.sha256(
                json.dumps(
                    [
                        (
                            s.run_id,
                            s.turn.source_hash,
                            s.started_at.isoformat(),
                            s.ended_at.isoformat(),
                        )
                        for s in records
                    ]
                ).encode()
            ).hexdigest()
            known = db.execute(
                "SELECT 1 FROM character_state_sources WHERE agent_id=? "
                "AND character_id=? AND package_hash=? AND source_hash=? "
                "AND state='completed' LIMIT 1",
                (*self.scope, signature),
            ).fetchone()
            return {
                "episode": episode,
                "records": records,
                "source_hash": signature,
                "known": bool(known),
            }

    def attempt(
        self, snapshot: dict, status: str, now: datetime, *, error="", metrics=None
    ) -> None:
        episode = snapshot["episode"]
        with self.store._transaction() as db:
            if (
                status in {"processing", "completed"}
                and self.episodes._episode(db, episode.id).revision != episode.revision
            ):
                raise HistoryError("Character Episode source changed.")
            db.execute(
                "INSERT INTO character_state_sources VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(agent_id,character_id,package_hash,episode_id,episode_revision) "
                "DO UPDATE SET state=excluded.state,error=excluded.error,metrics=excluded.metrics "
                "WHERE character_state_sources.state!='completed'",
                (
                    *self.scope,
                    episode.id,
                    episode.revision,
                    snapshot["source_hash"],
                    status,
                    error,
                    json.dumps(metrics or {}),
                ),
            )
            if status == "processing":
                db.execute(
                    "UPDATE character_states SET last_attempt_at=? WHERE agent_id=? "
                    "AND character_id=? AND package_hash=?",
                    (now.isoformat(), *self.scope),
                )

    def publish(
        self,
        snapshot: dict,
        state: CharacterState,
        expected_revision: int,
        policy: StatePolicy,
        metrics: dict,
    ) -> None:
        if state.character_id != self.character_id or state.revision != expected_revision + 1:
            raise HistoryError("Character state publication scope differs.")
        fresh = self.snapshot(snapshot["episode"].id, policy)
        if (
            fresh["source_hash"] != snapshot["source_hash"]
            or fresh["episode"].revision != snapshot["episode"].revision
        ):
            raise HistoryError("Character evidence changed before publication.")
        records = snapshot["records"]
        source = next((s for s in records if s.run_id == state.source_run_id), None)
        if source is None or source.ended_at != state.observed_at:
            raise HistoryError("Character state origin is not the cited source.")
        position = source.position
        with self.store._transaction() as db:
            current = db.execute(
                "SELECT revision,source_position,source_at FROM character_states "
                "WHERE agent_id=? AND character_id=? AND package_hash=?",
                self.scope,
            ).fetchone()
            if (
                current is None
                or current["revision"] != expected_revision
                or position <= current["source_position"]
                or (
                    current["source_at"]
                    and state.observed_at < datetime.fromisoformat(current["source_at"])
                )
            ):
                raise HistoryError("Character state is newer than this attempt.")
            db.execute(
                "UPDATE character_states SET revision=?,source_position=?,source_at=?,payload=? "
                "WHERE agent_id=? AND character_id=? AND package_hash=?",
                (
                    state.revision,
                    position,
                    state.observed_at.isoformat(),
                    state.model_dump_json(),
                    *self.scope,
                ),
            )
            episode = snapshot["episode"]
            changed = db.execute(
                "UPDATE character_state_sources SET state='completed',error='',metrics=? "
                "WHERE agent_id=? AND character_id=? AND package_hash=? "
                "AND episode_id=? AND episode_revision=? AND source_hash=? "
                "AND state='processing'",
                (
                    json.dumps(metrics),
                    *self.scope,
                    episode.id,
                    episode.revision,
                    snapshot["source_hash"],
                ),
            ).rowcount
            if changed != 1:
                raise HistoryError("Character source attempt changed.")

    def status(self) -> dict:
        with self.store._transaction() as db:
            counts = {
                row[0]: row[1]
                for row in db.execute(
                    "SELECT state,COUNT(*) FROM "
                    "character_state_sources WHERE agent_id=? AND character_id=? "
                    "AND package_hash=? "
                    "GROUP BY state",
                    self.scope,
                ).fetchall()
            }
            latest = db.execute(
                "SELECT state,error,metrics FROM character_state_sources "
                "WHERE agent_id=? AND character_id=? AND package_hash=? "
                "ORDER BY rowid DESC LIMIT 1",
                self.scope,
            ).fetchone()
        row = self.row()
        return {
            "character_id": self.character_id,
            "revision": row["revision"],
            "counts": counts,
            "last_attempt_at": row["last_attempt_at"],
            "source_at": row["source_at"],
            "last": dict(latest) if latest else None,
        }
