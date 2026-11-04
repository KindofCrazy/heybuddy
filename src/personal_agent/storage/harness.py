"""Scoped derived harness state; original conversation messages remain authoritative."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from personal_agent.core.execution import CURRENT_RUN, CURRENT_SCOPE, RunScope
from personal_agent.core.history import HistoryError
from personal_agent.core.text_window import text_window
from personal_agent.tools.registry import ToolExecutionError

SCHEMA = (
    """CREATE TABLE harness_records (
    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id), source TEXT NOT NULL,
    kind TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    "CREATE INDEX harness_scope ON harness_records(agent_id,conversation_id,source,kind)",
    """CREATE TABLE harness_events (
    id INTEGER PRIMARY KEY, root_id TEXT NOT NULL, run_id TEXT,
    record_id TEXT NOT NULL REFERENCES harness_records(id), kind TEXT NOT NULL,
    revision INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE tool_artifacts (
    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id), source TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(id), call_id TEXT NOT NULL, hash TEXT NOT NULL,
    body BLOB NOT NULL, complete INTEGER NOT NULL)""",
)


class HarnessRepository:
    def __init__(self, store) -> None:
        self.store = store

    @staticmethod
    def scope() -> RunScope:
        scope = CURRENT_SCOPE.get()
        if scope is None:
            raise ToolExecutionError("scope_required", "A trusted execution scope is required.")
        return scope

    def list(self, kind: str, limit: int = 100) -> list[dict]:
        scope = self.scope()
        with self.store._transaction() as db:
            rows = db.execute(
                "SELECT id,revision,payload FROM harness_records WHERE agent_id=? "
                "AND conversation_id=? AND source=? AND kind=? "
                "ORDER BY updated_at DESC,rowid DESC LIMIT ?",
                (scope.agent_id, scope.conversation_id, scope.source, kind, min(limit, 100)),
            ).fetchall()
        return [dict(json.loads(r["payload"]), id=r["id"], revision=r["revision"]) for r in rows]

    def get(self, identifier: str, kind: str) -> dict:
        scope = self.scope()
        with self.store._transaction() as db:
            row = db.execute(
                "SELECT revision,payload FROM harness_records WHERE id=? AND kind=? "
                "AND agent_id=? AND conversation_id=? AND source=?",
                (identifier, kind, scope.agent_id, scope.conversation_id, scope.source),
            ).fetchone()
        if row is None:
            raise ToolExecutionError("not_found", "Record is unavailable in this source.")
        return dict(json.loads(row["payload"]), id=identifier, revision=row["revision"])

    def notifications(self) -> list[dict]:
        scope = self.scope()
        with self.store.reading() as db:
            rows = db.execute(
                "SELECT id,revision,payload FROM harness_records WHERE agent_id=? "
                "AND conversation_id=? AND source=? AND kind='execution' "
                "AND json_extract(payload,'$.notified')=0 AND "
                "json_extract(payload,'$.status') IN ('succeeded','failed','interrupted') "
                "ORDER BY rowid LIMIT 16",
                (scope.agent_id, scope.conversation_id, scope.source),
            ).fetchall()
        return [
            dict(json.loads(row["payload"]), id=row["id"], revision=row["revision"]) for row in rows
        ]

    def put(self, kind: str, value: dict, *, identifier: str | None = None) -> dict:
        scope = self.scope()
        identifier = identifier or uuid4().hex
        revision = value.get("revision", 0)
        contents = {k: v for k, v in value.items() if k not in {"id", "revision"}}
        if kind == "run_summary":
            run_id = CURRENT_RUN.get()
            with self.store.reading() as db:
                first = db.execute(
                    "SELECT min(sequence) FROM messages WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            contents.update(
                run_id=run_id,
                first_sequence=(first or 0) + 1,
                through_sequence=(first or 0) + value["through"] - 1,
            )
        payload = json.dumps(contents, ensure_ascii=False)
        if len(payload.encode()) > 131072:
            raise ToolExecutionError("record_limit", "Record exceeds storage budget.")
        with self.store._transaction() as db:
            if revision:
                changed = db.execute(
                    "UPDATE harness_records SET payload=?,revision=revision+1,"
                    "updated_at=CURRENT_TIMESTAMP WHERE id=? AND revision=? AND kind=? "
                    "AND agent_id=? AND conversation_id=? AND source=?",
                    (
                        payload,
                        identifier,
                        revision,
                        kind,
                        scope.agent_id,
                        scope.conversation_id,
                        scope.source,
                    ),
                ).rowcount
                if not changed:
                    raise ToolExecutionError("conflict", "Record revision changed.")
            else:
                db.execute(
                    "INSERT INTO harness_records(id,agent_id,conversation_id,source,kind,"
                    "revision,payload) VALUES(?,?,?,?,?,1,?)",
                    (
                        identifier,
                        scope.agent_id,
                        scope.conversation_id,
                        scope.source,
                        kind,
                        payload,
                    ),
                )
            db.execute(
                "INSERT INTO harness_events(root_id,run_id,record_id,kind,revision) "
                "VALUES(?,?,?,?,?)",
                (scope.root_id, CURRENT_RUN.get(), identifier, kind, revision + 1),
            )
        return dict(json.loads(payload), id=identifier, revision=revision + 1)

    def recover(self) -> None:
        # Called once under the database owner lock. Never launch operations.
        with self.store._transaction() as db:
            db.execute(
                "UPDATE harness_records SET payload=json_set(payload,'$.status','interrupted'),"
                "revision=revision+1 WHERE kind IN ('task','execution','workflow') "
                "AND json_extract(payload,'$.status') IN ('running','in_progress','queued')"
            )

    def save_artifact(self, call_id: str, body: bytes) -> dict:
        scope, run_id = self.scope(), CURRENT_RUN.get()
        if run_id is None:
            raise HistoryError("Artifact requires an active journal run.")
        complete = len(body) <= 1048576
        if not complete:
            body = body[:1048576].decode("utf-8", "ignore").encode("utf-8")
        digest, identifier = hashlib.sha256(body).hexdigest(), uuid4().hex
        with self.store._transaction() as db:
            db.execute(
                "INSERT INTO tool_artifacts VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    scope.agent_id,
                    scope.conversation_id,
                    scope.source,
                    run_id,
                    call_id,
                    digest,
                    body,
                    int(complete),
                ),
            )
        return dict(artifact_id=identifier, hash=digest, size_bytes=len(body), complete=complete)

    def artifact(self, identifier: str, offset: int = 0, limit: int = 16384) -> dict:
        scope = self.scope()
        if offset < 0 or not 1 <= limit <= 16384:
            raise ToolExecutionError("invalid_window", "Invalid artifact window.")
        with self.store._transaction() as db:
            row = db.execute(
                "SELECT hash,length(body) AS size,substr(body,?,?) AS body,complete "
                "FROM tool_artifacts WHERE id=? AND agent_id=? AND conversation_id=? AND source=?",
                (
                    offset + 1,
                    limit,
                    identifier,
                    scope.agent_id,
                    scope.conversation_id,
                    scope.source,
                ),
            ).fetchone()
        if row is None:
            raise ToolExecutionError("not_found", "Artifact is unavailable in this source.")
        try:
            text, used = text_window(
                bytes(row["body"]), final=offset + len(row["body"]) >= row["size"]
            )
        except (ValueError, UnicodeError):
            raise ToolExecutionError(
                "invalid_window", "Choose a UTF-8 character boundary and sufficient limit."
            ) from None
        return dict(
            artifact_id=identifier,
            hash=row["hash"],
            size_bytes=row["size"],
            text=text,
            complete=bool(row["complete"]),
            next_offset=offset + used if offset + used < row["size"] else None,
        )
