"""Immutable Agent-scoped journal attachments in the existing SQLite database."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from personal_agent.core.history import HistoryError
from personal_agent.core.images import ImageRef

if TYPE_CHECKING:
    from personal_agent.storage.sqlite import SQLiteStore

SCHEMA = (
    """CREATE TABLE image_attachments (
        agent_id TEXT NOT NULL REFERENCES agents(id), sha256 TEXT NOT NULL,
        metadata TEXT NOT NULL, data BLOB NOT NULL CHECK(length(data) BETWEEN 1 AND 262144),
        PRIMARY KEY(agent_id,sha256))""",
    "ALTER TABLE qq_receipts ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'",
)


class SQLiteImages:
    def __init__(self, store: SQLiteStore, agent_id: str) -> None:
        self.store = store
        self.agent_id = agent_id

    def save_image(self, data: bytes, width: int, height: int) -> ImageRef:
        ref = ImageRef(
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            width=width,
            height=height,
        )
        with self.store._transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO image_attachments VALUES (?,?,?,?)",
                (self.agent_id, ref.sha256, ref.model_dump_json(), data),
            )
        return ref

    def read_image(self, reference: ImageRef) -> bytes:
        with self.store.reading() as db:
            row = db.execute(
                "SELECT metadata,data FROM image_attachments WHERE agent_id=? AND sha256=?",
                (self.agent_id, reference.sha256),
            ).fetchone()
        if row is None:
            raise HistoryError("Image attachment is unavailable in this Agent.")
        if json.loads(row[0]) != reference.model_dump():
            raise HistoryError("Image attachment metadata differs.")
        data = bytes(row[1])
        if (
            len(data) != reference.size_bytes
            or hashlib.sha256(data).hexdigest() != reference.sha256
        ):
            raise HistoryError("Image attachment integrity check failed.")
        return data
