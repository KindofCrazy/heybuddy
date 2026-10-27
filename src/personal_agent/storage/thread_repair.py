"""Bounded rebuild checkpoints over the CURRENT membership of one dirty Thread."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from personal_agent.core.history import HistoryError
from personal_agent.organization.models import OrganizationError, RepairPage, ThreadRepair
from personal_agent.storage.threads import ThreadRepository


class ThreadRepairRepository:
    def __init__(self, threads: ThreadRepository) -> None:
        self.threads = threads
        self.store = threads.store
        self.agent_id = threads.agent_id

    def pending(
        self, *, retry: bool = False, at: datetime | None = None, max_failures: int = 3
    ) -> str | None:
        with self.store._transaction() as db:
            row = db.execute(
                "SELECT t.id FROM topic_threads t LEFT JOIN thread_repairs r ON r.thread_id=t.id "
                "WHERE t.agent_id=? AND t.dirty=1 AND (? OR r.thread_id IS NULL OR "
                "r.source_revision!=t.source_revision OR r.thread_revision!=t.revision "
                "OR r.state NOT IN ('failed','running') OR (r.state='failed' "
                "AND r.failure_count<? AND r.retry_after<=?)) "
                "ORDER BY t.last_active_at,t.id LIMIT 1",
                (self.agent_id, retry, max_failures, (at or datetime.now(UTC)).isoformat()),
            ).fetchone()
            return row[0] if row else None

    def _page(self, db: sqlite3.Connection, thread_id: str, limit: int, cap: int) -> RepairPage:
        thread = self.threads._thread(db, thread_id)
        job = db.execute("SELECT * FROM thread_repairs WHERE thread_id=?", (thread_id,)).fetchone()
        rows = db.execute(
            "SELECT e.id,e.ended_at,length(CAST(e.payload AS BLOB)) AS size FROM episodes e "
            "JOIN conversations c ON c.id=e.stream_id WHERE e.thread_id=? AND c.agent_id=? "
            "AND (e.ended_at>? OR (e.ended_at=? AND e.id>?)) ORDER BY e.ended_at,e.id LIMIT ?",
            (
                thread_id,
                self.agent_id,
                job["after_time"],
                job["after_time"],
                job["after_id"],
                limit + 1,
            ),
        ).fetchall()
        episodes = []
        used = 0
        for row in rows:
            if len(episodes) >= limit or used + row["size"] > cap:
                if not episodes:
                    raise OrganizationError("repair_source_budget")
                break
            episodes.append(self.threads.episodes._episode(db, row["id"]))
            used += row["size"]
        return RepairPage(
            thread=thread,
            revision=job["revision"],
            after_time=job["after_time"],
            after_id=job["after_id"],
            working_state=job["working_state"],
            source_count=job["source_count"],
            episodes=episodes,
            has_more=len(rows) > len(episodes),
        )

    def window(
        self,
        thread_id: str,
        limit: int,
        cap: int,
        *,
        retry: bool,
        at: datetime | None = None,
        max_failures: int = 3,
    ) -> RepairPage | None:
        if not 1 <= limit <= 16 or not 1024 <= cap <= 262144:
            raise ValueError("Invalid repair page budget")
        with self.store._transaction() as db:
            thread = self.threads._thread(db, thread_id)
            if not thread.dirty:
                return None
            job = db.execute(
                "SELECT * FROM thread_repairs WHERE thread_id=?", (thread_id,)
            ).fetchone()
            reset = (
                not job
                or job["source_revision"] != thread.source_revision
                or job["thread_revision"] != thread.revision
                or job["state"] == "completed"
            )
            if reset:
                db.execute("DELETE FROM thread_repair_sources WHERE thread_id=?", (thread_id,))
                db.execute(
                    "INSERT INTO thread_repairs (thread_id,source_revision,thread_revision,"
                    "revision,"
                    "updated_at) VALUES (?,?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET "
                    "source_revision=excluded.source_revision,thread_revision=excluded.thread_revision,"
                    "revision=excluded.revision,after_time='',after_id='',working_state='',"
                    "source_count=0,state='pending',error_code='',metrics='{}',"
                    "failure_count=0,retry_after=NULL,updated_at=excluded.updated_at",
                    (
                        thread_id,
                        thread.source_revision,
                        thread.revision,
                        job["revision"] + 1 if job else 1,
                        datetime.now(UTC).isoformat(),
                    ),
                )
            elif job["state"] in {"failed", "running"}:
                if not retry and (
                    job["state"] == "running"
                    or job["failure_count"] >= max_failures
                    or not job["retry_after"]
                    or datetime.fromisoformat(job["retry_after"]) > (at or datetime.now(UTC))
                ):
                    return None
                if retry:
                    db.execute(
                        "UPDATE thread_repairs SET failure_count=0,retry_after=NULL "
                        "WHERE thread_id=?",
                        (thread_id,),
                    )
            return self._page(db, thread_id, limit, cap)

    def attempt(
        self, page: RepairPage, state: str, code: str = "", metrics: dict | None = None
    ) -> None:
        with self.store._transaction() as db:
            current = self.threads._thread(db, page.thread.id)
            if current != page.thread:
                raise HistoryError("Thread repair source changed.")
            changed = db.execute(
                "UPDATE thread_repairs SET state=?,error_code=?,metrics=?,updated_at=? "
                "WHERE thread_id=? AND revision=? AND source_revision=?",
                (
                    state,
                    code,
                    json.dumps(metrics or {}),
                    datetime.now(UTC).isoformat(),
                    page.thread.id,
                    page.revision,
                    page.thread.source_revision,
                ),
            ).rowcount
            if changed != 1:
                raise HistoryError("Thread repair checkpoint changed.")

    def fail(
        self, thread_id: str, code: str, *, at: datetime | None = None, retry_seconds: float = 300
    ) -> None:
        with self.store._transaction() as db:
            thread = self.threads._thread(db, thread_id)
            job = db.execute(
                "SELECT failure_count FROM thread_repairs WHERE thread_id=?", (thread_id,)
            ).fetchone()
            delay = min(86400, retry_seconds * 2 ** min(job[0] if job else 0, 8))
            db.execute(
                "UPDATE thread_repairs SET state='failed',error_code=?,"
                "failure_count=failure_count+1,"
                "retry_after=? "
                "WHERE thread_id=? AND source_revision=? AND thread_revision=?",
                (
                    code,
                    ((at or datetime.now(UTC)) + timedelta(seconds=delay)).isoformat(),
                    thread_id,
                    thread.source_revision,
                    thread.revision,
                ),
            )

    def publish(
        self, page: RepairPage, decision: ThreadRepair | None, limit: int, cap: int, metrics: dict
    ) -> bool:
        with self.store._transaction() as db:
            if self._page(db, page.thread.id, limit, cap) != page:
                raise HistoryError("Thread repair membership or checkpoint changed.")
            if not page.episodes and (page.source_count or page.has_more):
                raise HistoryError("Thread repair source window is inconsistent.")
            if bool(page.episodes) != (decision is not None):
                raise HistoryError("Thread repair output does not match its sources.")
            for index, episode in enumerate(page.episodes, page.source_count):
                db.execute(
                    "INSERT INTO thread_repair_sources VALUES (?,?,?,?)",
                    (page.thread.id, episode.id, episode.revision, index),
                )
            last = page.episodes[-1] if page.episodes else None
            state = decision.current_state if decision else "当前没有关联的 Episode。"
            if page.has_more:
                db.execute(
                    "UPDATE thread_repairs SET revision=revision+1,after_time=?,after_id=?,"
                    "working_state=?,source_count=?,state='pending',error_code='',"
                    "metrics=?,updated_at=? "
                    "WHERE thread_id=?",
                    (
                        last.ended_at.isoformat(),
                        last.id,
                        state,
                        page.source_count + len(page.episodes),
                        json.dumps(metrics),
                        datetime.now(UTC).isoformat(),
                        page.thread.id,
                    ),
                )
                return False
            value = page.thread.model_copy(
                update={
                    "revision": page.thread.revision + 1,
                    "current_state": state,
                    "dirty": False,
                    "status": decision.status if decision else "closed",
                    "last_active_at": last.ended_at if last else page.thread.last_active_at,
                }
            )
            raw = value.model_dump_json()
            db.execute(
                "UPDATE topic_threads SET revision=?,dirty=0,last_active_at=?,payload=? WHERE id=?",
                (value.revision, value.last_active_at.isoformat(), raw, value.id),
            )
            db.execute(
                "INSERT INTO thread_versions VALUES (?,?,?,?)",
                (value.id, value.revision, raw, "[]"),
            )
            db.execute(
                "INSERT INTO thread_state_sources "
                "SELECT thread_id,?,episode_id,episode_revision,ordinal "
                "FROM thread_repair_sources WHERE thread_id=?",
                (value.revision, value.id),
            )
            db.execute(
                "UPDATE thread_repairs SET revision=revision+1,working_state=?,source_count=?,"
                "state='completed',error_code=?,metrics=?,updated_at=? WHERE thread_id=?",
                (
                    state,
                    page.source_count + len(page.episodes),
                    "" if decision else "empty_archived",
                    json.dumps(metrics),
                    datetime.now(UTC).isoformat(),
                    value.id,
                ),
            )
            return True
