import asyncio
import logging

from personal_agent.core.context import ContextBuilder
from personal_agent.core.history import HistoryError
from personal_agent.core.memory import CoreMemoryError
from personal_agent.core.models import (
    AssistantMessage,
    Message,
    RunResult,
    ToolError,
    ToolMessage,
    ToolResult,
)
from personal_agent.llm.base import Provider, ProviderError, ProviderTimeout
from personal_agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class Agent:
    """A bounded handwritten loop. One instance represents one conversation session."""

    def __init__(
        self,
        provider: Provider,  # communicate with LLM
        tools: ToolRegistry,  # register, validate and execute tools
        context: ContextBuilder | None = None,  # build context
        *,
        max_rounds: int = 10,
        request_timeout_seconds: float = 60,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")
        if not 0 < request_timeout_seconds < float("inf"):
            raise ValueError("Request timeout must be positive and finite")
        self.provider = provider
        self.tools = tools
        self.context = context if context is not None else ContextBuilder()
        self.max_rounds = max_rounds
        self.request_timeout_seconds = request_timeout_seconds
        self._lock = asyncio.Lock()

    async def run(self, user_input: str, *, permissions: frozenset[str] = frozenset()) -> RunResult:
        """
        External Lifecycle
        """

        if not user_input.strip():
            raise ValueError("User input cannot be empty")
        permissions = frozenset(permissions)
        # Serialize turns on this session to prevent interleaved history.
        async with self._lock:
            self._round_number = 0
            try:
                messages = self.context.begin(user_input)
                return await self._run(messages, permissions=permissions)
            except HistoryError:
                self._abort_context()
                logger.error("history_failed round=%d", self._round_number)
                return RunResult(
                    status="storage_error",
                    rounds=self._round_number,
                    error="Conversation history could not be saved or loaded; "
                    "run stopped without retrying tools. Tool effects may already have occurred.",
                )
            except asyncio.CancelledError:
                self._abort_context(cancelled=True)
                raise  # raise error to let the external layers know

    def _abort_context(self, *, cancelled: bool = False) -> None:
        try:
            self.context.abort(cancelled=cancelled)
        except HistoryError:
            logger.error("history_interrupt_failed")

    async def _run(self, messages: list[Message], *, permissions: frozenset[str]) -> RunResult:
        """
        Internal Agent Loop
        """

        def append(message: Message) -> None:
            # Save assistant calls before any execution and each result before the
            # next tool. A write failure stops the run; external effects cannot roll back.
            self.context.record(message)
            messages.append(message)

        def finish(result: RunResult) -> RunResult:
            if result.status != "completed":
                # Keep completed tool effects and outputs in history. No automatic retry
                # or pretend rollback; every recorded call already has a result.
                append(AssistantMessage(text=f"[Runtime stopped: {result.status}] {result.error}"))
            self.context.commit(messages, result)
            logger.info("run_end status=%s rounds=%d", result.status, result.rounds)
            return result

        for round_number in range(1, self.max_rounds + 1):
            self._round_number = round_number
            logger.info("model_request round=%d", round_number)
            try:
                async with asyncio.timeout(self.request_timeout_seconds):
                    # Reserve the last request for a final answer. Also guard below
                    # against unsolicited calls; tool omission alone is not enforcement.
                    definitions = (
                        self.tools.definitions(permissions=permissions)
                        if round_number < self.max_rounds
                        else []
                    )
                    response = await self.provider.generate(
                        self.context.for_request(messages), definitions
                    )
            except CoreMemoryError as exc:
                return finish(
                    RunResult(
                        status="memory_error",
                        rounds=round_number,
                        error=f"Core Memory could not be compiled ({exc.code}).",
                    )
                )
            except (TimeoutError, ProviderTimeout):
                return finish(
                    RunResult(
                        status="timeout", rounds=round_number, error="Model request timed out."
                    )
                )
            except ProviderError as exc:
                logger.warning("provider_failed code=%s http_status=%s", exc.code, exc.status_code)
                detail = f"{exc.code}, HTTP {exc.status_code}" if exc.status_code else exc.code
                return finish(
                    RunResult(
                        status="provider_error",
                        rounds=round_number,
                        error=f"Model provider failed ({detail}); check access and network.",
                    )
                )
            except Exception as exc:
                logger.error("runtime_failed type=%s", type(exc).__name__)
                return finish(
                    RunResult(
                        status="runtime_error",
                        rounds=round_number,
                        error="Unexpected provider failure; see diagnostic logs.",
                    )
                )

            # Reject ambiguous IDs before executing anything in this response.
            seen = {
                call.id
                for message in messages
                if isinstance(message, AssistantMessage)
                for call in message.tool_calls
            }
            for call in response.tool_calls:
                if call.id in seen:
                    return finish(
                        RunResult(
                            status="invalid_response",
                            rounds=round_number,
                            error="Model returned a duplicate tool call ID.",
                        )
                    )
                seen.add(call.id)

            if not response.tool_calls and not response.text.strip():
                return finish(
                    RunResult(
                        status="invalid_response",
                        rounds=round_number,
                        error="Model returned neither text nor tool calls.",
                    )
                )

            append(
                AssistantMessage(
                    text=response.text,
                    tool_calls=response.tool_calls,
                    provider_data=response.provider_data,
                )
            )
            logger.info(
                "model_response round=%d tool_calls=%d", round_number, len(response.tool_calls)
            )
            if not response.tool_calls:
                return finish(
                    RunResult(status="completed", text=response.text, rounds=round_number)
                )

            if round_number == self.max_rounds:
                logger.warning(
                    "tool_calls_rejected reason=round_budget_exhausted count=%d",
                    len(response.tool_calls),
                )
                # Record explicit rejections instead of leaving dangling calls in
                # the session. No validation or execution is delegated to the registry.
                for call in response.tool_calls:
                    append(
                        ToolMessage(
                            result=ToolResult(
                                call_id=call.id,
                                name=call.name,
                                ok=False,
                                error=ToolError(
                                    code="round_budget_exhausted",
                                    message="Tool was not executed: no request budget "
                                    "remains to process its result.",
                                ),
                            )
                        )
                    )
                break

            # Sequential execution is deliberate: side effects have a predictable order.
            # ToolRegistry turns validation, execution, and timeout failures into data.
            for call in response.tool_calls:
                append(ToolMessage(result=await self.tools.execute(call, permissions=permissions)))

        return finish(
            RunResult(
                status="max_rounds",
                rounds=self.max_rounds,
                error="Model round limit reached; final-round tools were not executed.",
            )
        )
