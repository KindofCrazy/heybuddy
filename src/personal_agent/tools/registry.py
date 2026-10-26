import asyncio
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError

from personal_agent.core.models import ToolCall, ToolDefinition, ToolError, ToolResult

logger = logging.getLogger(__name__)
JSON_VALUE = TypeAdapter(JsonValue)


class ToolExecutionError(Exception):
    """A tool's intentional, sanitized domain failure."""

    def __init__(self, code: str, message: str, *, data: JsonValue = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class ToolArguments(BaseModel):
    """Strict tool inputs: reject unknown fields, coercion, and non-finite numbers."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


@dataclass(frozen=True)
class Tool[Args: BaseModel]:
    """Sync functions require an explicit no-side-effects contract, not cancellation support."""

    name: str
    description: str
    arguments: type[Args]
    function: Callable[[Args], object | Awaitable[object]]
    side_effect_free: bool = field(default=False, kw_only=True)
    required_permission: str | None = field(default=None, kw_only=True)


class ToolRegistry:
    def __init__(self, timeout_seconds: float = 10) -> None:
        if not 0 < timeout_seconds < float("inf"):
            raise ValueError("Tool timeout must be positive and finite")
        self.timeout_seconds = timeout_seconds
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool.name):
            raise ValueError("Tool name must contain 1-64 letters, digits, underscores or hyphens")
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        if not inspect.iscoroutinefunction(tool.function) and not tool.side_effect_free:
            raise ValueError(
                "Synchronous tools must declare side_effect_free=True: "
                "thread timeouts cannot cancel execution or roll back side effects"
            )
        self._tools[tool.name] = tool

    def definitions(self, *, permissions: frozenset[str] = frozenset()) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name=tool.name,
                description=tool.description,
                parameters=tool.arguments.model_json_schema(),
            )
            for tool in self._tools.values()
            if tool.required_permission is None or tool.required_permission in permissions
        ]

    async def execute(
        self, call: ToolCall, *, permissions: frozenset[str] = frozenset()
    ) -> ToolResult:
        def failure(code: str, message: str, *, data: JsonValue = None) -> ToolResult:
            logger.warning("tool_failed name=%s call_id=%s code=%s", call.name, call.id, code)
            return ToolResult(
                call_id=call.id,
                name=call.name,
                ok=False,
                data=data,
                error=ToolError(code=code, message=message),
            )

        # CHECK 1: Is tool existed?
        tool = self._tools.get(call.name)
        if tool is None:
            return failure("unknown_tool", "Tool is not registered.")
        # CHECK 2: Permission?
        if tool.required_permission is not None and tool.required_permission not in permissions:
            return failure(
                "permission_denied", "This tool requires explicit permission for this run."
            )
        # CHECK 3: Pydantic parameters
        try:
            arguments = tool.arguments.model_validate_json(call.arguments)
        except ValidationError as exc:
            # Do not include input values or custom-validator messages in diagnostics.
            issues = "; ".join(
                f"{'.'.join(map(str, issue['loc'])) or '$'}: {issue['type']}"
                for issue in exc.errors(
                    include_input=False, include_context=False, include_url=False
                )
            )
            return failure("invalid_arguments", issues)
        except Exception as exc:
            logger.warning("argument_validation_failed type=%s", type(exc).__name__)
            return failure("invalid_arguments", "Tool argument validation failed.")

        logger.info("tool_start name=%s call_id=%s", call.name, call.id)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                if inspect.iscoroutinefunction(tool.function):
                    value = await tool.function(arguments)
                else:
                    # Cancelling this await does NOT stop a running thread. Only
                    # explicitly side-effect-free sync functions may be registered.
                    value = await asyncio.to_thread(tool.function, arguments)
                    if inspect.isawaitable(value):
                        value = await value
                if isinstance(value, BaseModel):
                    value = value.model_dump(mode="json")
                data = JSON_VALUE.validate_python(value)
                # Reject NaN/Infinity rather than silently emitting invalid JSON or null.
                json.dumps(data, allow_nan=False)
        except TimeoutError:
            if not inspect.iscoroutinefunction(tool.function):
                return failure(
                    "tool_timeout",
                    "Stopped waiting for the synchronous tool; its thread may still run. "
                    "Completion is unknown. Side effects are not cancelled or rolled back; "
                    "do not automatically retry.",
                )
            return failure(
                "tool_timeout",
                "Tool execution timed out; cancellation does not guarantee side-effect rollback. "
                "Do not automatically retry.",
            )
        except ToolExecutionError as exc:
            try:
                error_data = JSON_VALUE.validate_python(exc.data)
                json.dumps(error_data, allow_nan=False)
            except (ValidationError, TypeError, ValueError):
                return failure("execution_error", "Tool returned invalid failure data.")
            return failure(exc.code, str(exc), data=error_data)
        except Exception as exc:
            logger.warning(
                "tool_exception name=%s call_id=%s type=%s",
                call.name,
                call.id,
                type(exc).__name__,
            )
            return failure("execution_error", "Tool failed to execute or return a JSON value.")

        logger.info("tool_end name=%s call_id=%s", call.name, call.id)
        return ToolResult(call_id=call.id, name=call.name, ok=True, data=data)
