"""Thread-filtered replay of original Conversation Runs, with independent compaction."""

from personal_agent.core.history import HistoryError, HistoryPage
from personal_agent.storage import context
from personal_agent.storage.history import SQLiteConversationHistory


class SQLiteThreadHistory(SQLiteConversationHistory):
    def __init__(self, store, conversation_id: str) -> None:
        super().__init__(store, conversation_id)
        self.thread_id: str | None = None
        self.source = "cli"

    def summary_scope(self):
        return {"thread_id": self.thread_id, "source": self.source}

    def begin(self, user_message):
        self.thread_id = None
        return super().begin(user_message)

    def summary(self):
        if self.thread_id is None:
            return None
        with self.store.reading() as db:
            return context.summary(
                db, self.conversation_id, thread_id=self.thread_id, source=self.source
            )

    def page(self, after, *, through=None, max_runs=32, max_bytes=262144):
        if self.thread_id is None:
            return HistoryPage(turns=[])
        with self.store.reading() as db:
            return context.page(
                db,
                self.conversation_id,
                after,
                through=through,
                max_runs=max_runs,
                max_bytes=max_bytes,
                thread_id=self.thread_id,
                source=self.source,
            )

    def compact_through(self, recent_runs):
        if self.thread_id is None:
            return 0
        with self.store.reading() as db:
            return context.compact_through(
                db, self.conversation_id, recent_runs, thread_id=self.thread_id, source=self.source
            )

    def save_summary(self, text, previous, turns):
        if self.thread_id is None:
            raise HistoryError("Thread must be selected before compaction.")
        with self.store._transaction() as db:
            return context.save_summary(
                db,
                self.conversation_id,
                text,
                previous,
                turns,
                thread_id=self.thread_id,
                source=self.source,
            )
