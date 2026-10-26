"""Bind a conversation ID to the database-independent history contract."""

from personal_agent.core.history import ConversationSummary, HistoryError, HistoryPage, HistoryTurn
from personal_agent.core.models import Message, RunResult, TextMessage
from personal_agent.storage.sqlite import SQLiteStore


class SQLiteConversationHistory:
    def __init__(self, store: SQLiteStore, conversation_id: str) -> None:
        self.store = store
        self.conversation_id = conversation_id

    def load(self) -> list[Message]:
        previous = self.summary()
        result = self.page(previous.through_sequence if previous else 0)
        if result.has_more:
            raise HistoryError("History requires bounded context preparation.")
        return result.messages

    def summary(self) -> ConversationSummary | None:
        return self.store.context_summary(self.conversation_id)

    def page(
        self, after: int, *, through: int | None = None, max_runs: int = 32, max_bytes: int = 262144
    ) -> HistoryPage:
        return self.store.context_page(
            self.conversation_id, after, through=through, max_runs=max_runs, max_bytes=max_bytes
        )

    def compact_through(self, recent_runs: int) -> int:
        return self.store.compact_through(self.conversation_id, recent_runs)

    def save_summary(
        self, text: str, previous: ConversationSummary | None, turns: list[HistoryTurn]
    ) -> ConversationSummary:
        return self.store.save_context_summary(self.conversation_id, text, previous, turns)

    def begin(self, user_message: TextMessage) -> str:
        return self.store.begin_run(self.conversation_id, user_message)

    def append(self, run_id: str, message: Message) -> None:
        self.store.append_message(run_id, message)

    def finish(self, run_id: str, result: RunResult) -> None:
        self.store.finish_run(run_id, result)

    def interrupt(self, run_id: str, *, cancelled: bool = False) -> None:
        self.store.interrupt_run(run_id, cancelled=cancelled)
