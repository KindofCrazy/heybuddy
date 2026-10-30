"""Local session selection. Conversation history is not cross-conversation memory."""

from collections.abc import Callable

from personal_agent.core.budget import ContextBudget
from personal_agent.core.character import CharacterContext
from personal_agent.core.context import DEFAULT_SYSTEM_PROMPT, ContextBuilder
from personal_agent.core.history import HistoryError
from personal_agent.core.memory import CoreMemory
from personal_agent.core.outbound import ConversationNotices
from personal_agent.core.retrieval import ContextRetrieval
from personal_agent.core.temporal import temporal_context
from personal_agent.storage.history import SQLiteConversationHistory
from personal_agent.storage.models import ConversationRecord
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.storage.tracing import SQLiteRunTrace


class SessionManager:
    def __init__(
        self,
        store: SQLiteStore,
        *,
        memory: CoreMemory | None = None,
        budget: ContextBudget | None = None,
        optional_history_tokens: int = 12288,
        character: CharacterContext | None = None,
    ) -> None:
        self.read_only = store.read_only
        self.budget = budget
        self.optional_history_tokens = optional_history_tokens
        self.store = store
        self.memory = memory
        self.character = character
        self.retriever: ContextRetrieval | None = None
        self.notices: ConversationNotices | None = None
        self.notice_factory: Callable[[str], ConversationNotices] | None = None
        self.agent = store.get_agent("heybuddy")
        if self.agent is None:
            if self.read_only:
                raise HistoryError("Observer requires an existing Agent.")
            self.agent = store.create_agent("HeyBuddy", DEFAULT_SYSTEM_PROMPT, agent_id="heybuddy")
        previous = store.last_conversation(self.agent.id)
        if self.read_only and not previous:
            raise HistoryError("Observer requires an existing selected Conversation.")
        self.current = self.resume(previous) if previous else self.new()

    def context(self) -> ContextBuilder:
        conversation_id = self.current.id
        return ContextBuilder(
            DEFAULT_SYSTEM_PROMPT,
            history_store=SQLiteConversationHistory(self.store, conversation_id),
            memory=self.memory,
            budget=self.budget,
            temporal=lambda: temporal_context(self.store.last_interaction(conversation_id)),
            retriever=self.retriever,
            optional_history_tokens=self.optional_history_tokens,
            character=self.character,
            trace=SQLiteRunTrace(self.store),
            notices=self.notice_factory(conversation_id) if self.notice_factory else self.notices,
        )

    def new(self) -> ConversationRecord:
        if self.read_only:
            raise HistoryError("Observer cannot create a Conversation.")
        self.current = self.store.create_conversation(self.agent.id, activate=True)
        return self.current

    def resume(self, conversation_id: str) -> ConversationRecord:
        conversation = self.store.get_conversation(conversation_id)
        if conversation.agent_id != self.agent.id:
            raise HistoryError("Conversation belongs to a different agent.")
        # Validate the checkpoint, not every historic payload. Pages are checked
        # before model use; ordinary writes already validate complete Runs.
        self.store.context_summary(conversation_id)
        if not self.read_only:
            self.store.select_conversation(self.agent.id, conversation.id)
        self.current = conversation
        return conversation

    def list(self) -> list[ConversationRecord]:
        return self.store.list_conversations(self.agent.id)

    def status(self) -> str:
        count, interrupted, last = self.store.run_statistics(self.current.id)
        return (
            f"Agent: {self.agent.id} ({self.agent.name})\nConversation: {self.current.id}\n"
            f"Runs: {count} | Last: {last} | Interrupted/cancelled: {interrupted}"
            + (
                "\n中断/取消 Run 仅保留审计，不进入模型上下文；副作用可能已发生，不会重试。"
                if interrupted
                else ""
            )
        )
