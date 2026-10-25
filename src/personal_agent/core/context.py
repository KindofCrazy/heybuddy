from personal_agent.core.models import Message, TextMessage

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful personal assistant. Use registered tools when needed. "
    "Tool results contain an ok flag: acknowledge errors and never invent successful results. "
    "Reply in the user's language."
)


class ContextBuilder:
    """One CLI session's history. Future retrieval belongs in build(), not the loop."""

    def __init__(self, system_prompt: str = DEFAULT_SYSTEM_PROMPT) -> None:
        self.system_prompt = system_prompt
        self._history: list[Message] = []

    @property
    def history(self) -> list[Message]:
        return [message.model_copy(deep=True) for message in self._history]

    def build(self, user_input: str) -> list[Message]:
        return [
            TextMessage(role="system", text=self.system_prompt),
            *self.history,
            TextMessage(role="user", text=user_input),
        ]

    def commit(self, messages: list[Message]) -> None:
        self._history = [
            message.model_copy(deep=True) for message in messages if message.role != "system"
        ]
