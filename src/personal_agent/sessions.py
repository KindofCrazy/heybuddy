"""Local session selection. Conversation history is not cross-conversation memory."""

from personal_agent.core.context import DEFAULT_SYSTEM_PROMPT, ContextBuilder
from personal_agent.core.history import HistoryError
from personal_agent.storage.history import SQLiteConversationHistory
from personal_agent.storage.models import ConversationRecord
from personal_agent.storage.sqlite import SQLiteStore


class SessionManager:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self.agent = store.get_agent("heybuddy") or store.create_agent(
            "HeyBuddy", DEFAULT_SYSTEM_PROMPT, agent_id="heybuddy"
        )
        previous = store.last_conversation(self.agent.id)
        self.current = self.resume(previous) if previous else self.new()

    def context(self) -> ContextBuilder:
        return ContextBuilder(
            self.agent.system_prompt,
            history_store=SQLiteConversationHistory(self.store, self.current.id),
        )

    def new(self) -> ConversationRecord:
        self.current = self.store.create_conversation(self.agent.id, activate=True)
        return self.current

    def resume(self, conversation_id: str) -> ConversationRecord:
        conversation = self.store.get_conversation(conversation_id)
        if conversation.agent_id != self.agent.id:
            raise HistoryError("Conversation belongs to a different agent.")
        # Validate replayable content before changing the durable active selection.
        self.store.load_messages(conversation_id, replayable_only=True)
        self.store.select_conversation(self.agent.id, conversation.id)
        self.current = conversation
        return conversation

    def list(self) -> list[ConversationRecord]:
        return self.store.list_conversations(self.agent.id)

    def status(self) -> str:
        runs = self.store.list_runs(self.current.id)
        interrupted = sum(run.status in {"cancelled", "interrupted"} for run in runs)
        last = runs[-1].status if runs else "none"
        return (
            f"Agent: {self.agent.id} ({self.agent.name})\nConversation: {self.current.id}\n"
            f"Runs: {len(runs)} | Last: {last} | Interrupted/cancelled: {interrupted}"
            + (
                "\n中断/取消 Run 仅保留审计，不进入模型上下文；副作用可能已发生，不会重试。"
                if interrupted
                else ""
            )
        )
