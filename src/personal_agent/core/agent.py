import asyncio
import logging
from time import perf_counter

from personal_agent.core.budget import ContextError
from personal_agent.core.context import ContextBuilder
from personal_agent.core.history import HistoryError
from personal_agent.core.images import ImageRef
from personal_agent.core.memory import CoreMemoryError
from personal_agent.core.models import (
    AssistantMessage,
    Message,
    ModelResponse,
    RunResult,
    ToolDefinition,
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
        max_tool_calls_per_response: int = 64,
        max_tool_calls_per_run: int = 256,
        request_timeout_seconds: float = 60,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")
        if max_tool_calls_per_response < 1 or max_tool_calls_per_run < 1:
            raise ValueError("Tool call limits must be positive")
        if not 0 < request_timeout_seconds < float("inf"):
            raise ValueError("Request timeout must be positive and finite")
        self.provider = provider
        self.tools = tools
        self.context = context if context is not None else ContextBuilder()
        self.max_rounds = max_rounds
        self.max_tool_calls_per_response = max_tool_calls_per_response
        self.max_tool_calls_per_run = max_tool_calls_per_run
        self.request_timeout_seconds = request_timeout_seconds
        self._lock = asyncio.Lock()

    async def run(
        self,
        user_input: str,
        *,
        permissions: frozenset[str] = frozenset(),
        images: list[ImageRef] | None = None,
    ) -> RunResult:
        """
        External Lifecycle
        """

        if not user_input.strip() and not images:
            raise ValueError("User input cannot be empty")
        permissions = frozenset(permissions)
        # Serialize turns on this session to prevent interleaved history.
        async with self._lock:
            self._round_number = 0
            try:
                messages = (
                    self.context.begin(user_input, images=images)
                    if images
                    else self.context.begin(user_input)
                )
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

        accepted_tool_calls = 0
        for round_number in range(1, self.max_rounds + 1):
            self._round_number = round_number
            logger.info("model_request round=%d", round_number)
            try:
                definitions = (
                    self.tools.definitions(permissions=permissions)
                    if round_number < self.max_rounds
                    else []
                )
                request = await self.context.prepare(messages, definitions, self.provider)
                options = (
                    {"max_output_tokens": self.context.budget.reserved_output_tokens}
                    if self.context.budget
                    else {}
                )
                response = await self._generate(request, definitions, options, round_number)
                self.context.observe_usage(response.usage)
            except ContextError as exc:
                return finish(
                    RunResult(
                        status="compaction_error"
                        if exc.code.startswith(("compaction_", "invalid_summary"))
                        else "context_error",
                        rounds=round_number,
                        error=f"Context preparation stopped ({exc.code}); "
                        "history and any completed tool effects were retained.",
                    )
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
            except HistoryError:
                raise
            except Exception as exc:
                logger.error("runtime_failed type=%s", type(exc).__name__)
                return finish(
                    RunResult(
                        status="runtime_error",
                        rounds=round_number,
                        error="Unexpected provider failure; see diagnostic logs.",
                    )
                )

            # Reject the whole over-limit batch before journaling or executing its calls.
            if (
                len(response.tool_calls) > self.max_tool_calls_per_response
                or accepted_tool_calls + len(response.tool_calls) > self.max_tool_calls_per_run
            ):
                return finish(
                    RunResult(
                        status="invalid_response",
                        rounds=round_number,
                        error="Host tool call limit exceeded; "
                        "no calls from this response executed.",
                    )
                )
            accepted_tool_calls += len(response.tool_calls)
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

    async def _generate(
        self,
        request: list[Message],
        definitions: list[ToolDefinition],
        options: dict,
        number: int,
    ) -> ModelResponse:
        trace, run_id = self.context.trace, self.context.run_id
        if trace is not None and run_id is not None:
            trace.start(run_id, number, request, definitions, options.get("max_output_tokens"))
        started = perf_counter()

        def complete(state: str, response: ModelResponse | None = None) -> None:
            if trace is not None and run_id is not None:
                trace.finish(
                    run_id, number, state, int((perf_counter() - started) * 1000), response
                )

        try:
            async with asyncio.timeout(self.request_timeout_seconds):
                response = await self.provider.generate(request, definitions, **options)
        except asyncio.CancelledError:
            try:
                complete("cancelled")
            except HistoryError:
                logger.error("request_audit_cancel_failed")
            raise
        except Exception as exc:
            state = (
                "timeout"
                if isinstance(exc, (TimeoutError, ProviderTimeout))
                else "provider_error"
                if isinstance(exc, ProviderError)
                else "runtime_error"
            )
            complete(state)
            raise
        complete("responded", response)
        return response
