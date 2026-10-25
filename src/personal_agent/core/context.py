from personal_agent.core.history import ConversationHistory, HistoryError
from personal_agent.core.models import Message, RunResult, TextMessage

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful personal assistant. Use registered tools when needed. "
    "Tool results contain an ok flag: acknowledge errors and never invent successful results. "
    "Reply in the user's language."
)


class ContextBuilder:
    """Replayable history, current context, and uncommitted run messages stay separate."""

    def __init__(
        self,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        *,
        history_store: ConversationHistory | None = None,
    ) -> None:
        self.system_prompt = system_prompt
        self._store = history_store
        self._history: list[Message] = []
        self._pending: list[Message] = []
        self._run_id: str | None = None
        self._active = False

    @property
    def history(self) -> list[Message]:
        history = self._store.load() if self._store is not None else self._history
        return [message.model_copy(deep=True) for message in history]

    @property
    def pending_messages(self) -> list[Message]:
        return [message.model_copy(deep=True) for message in self._pending]

    def build(self, user_input: str) -> list[Message]:
        return [
            TextMessage(role="system", text=self.system_prompt),
            *self.history,
            TextMessage(role="user", text=user_input),
        ]

    def begin(self, user_input: str) -> list[Message]:
        if self._active:
            raise HistoryError("Context already has an active run.")
        messages = self.build(user_input)
        user_message = TextMessage(role="user", text=user_input)
        if self._store is not None:
            self._run_id = self._store.begin(user_message)
        self._active = True
        self._pending = [user_message]
        return messages

    def record(self, message: Message) -> None:
        if not self._active:
            raise HistoryError("Context has no active run.")
        if self._store is not None:
            self._store.append(self._run_id, message)
        self._pending.append(message.model_copy(deep=True))

    def commit(self, messages: list[Message], result: RunResult) -> None:
        if self._store is not None:
            self._store.finish(self._run_id, result)
        self._history = [
            message.model_copy(deep=True) for message in messages if message.role != "system"
        ]
        self._pending = []
        self._run_id = None
        self._active = False

    def abort(self, *, cancelled: bool = False) -> None:
        try:
            if self._store is not None and self._run_id is not None:
                self._store.interrupt(self._run_id, cancelled=cancelled)
        finally:
            self._pending = []
            self._run_id = None
            self._active = False
