"""Bind a conversation ID to the database-independent history contract."""

from personal_agent.core.models import Message, RunResult, TextMessage
from personal_agent.storage.sqlite import SQLiteStore


class SQLiteConversationHistory:
    def __init__(self, store: SQLiteStore, conversation_id: str) -> None:
        self.store = store
        self.conversation_id = conversation_id

    def load(self) -> list[Message]:
        return [
            record.content
            for record in self.store.load_messages(self.conversation_id, replayable_only=True)
        ]

    def begin(self, user_message: TextMessage) -> str:
        return self.store.begin_run(self.conversation_id, user_message)

    def append(self, run_id: str, message: Message) -> None:
        self.store.append_message(run_id, message)

    def finish(self, run_id: str, result: RunResult) -> None:
        self.store.finish_run(run_id, result)

    def interrupt(self, run_id: str, *, cancelled: bool = False) -> None:
        self.store.interrupt_run(run_id, cancelled=cancelled)
