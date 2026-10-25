from datetime import UTC, datetime

from personal_agent.tools.registry import Tool, ToolArguments, ToolRegistry


class CurrentTimeArguments(ToolArguments):
    pass


class AddNumbersArguments(ToolArguments):
    a: float
    b: float


def get_current_time(_: CurrentTimeArguments) -> dict[str, str]:
    return {"time": datetime.now(UTC).isoformat(), "timezone": "UTC"}


def add_numbers(arguments: AddNumbersArguments) -> dict[str, float]:
    return {"sum": arguments.a + arguments.b}


def create_builtin_registry(timeout_seconds: float = 10) -> ToolRegistry:
    registry = ToolRegistry(timeout_seconds)
    registry.register(
        Tool(
            "get_current_time",
            "Get the current date and time in UTC.",
            CurrentTimeArguments,
            get_current_time,
            side_effect_free=True,
        )
    )
    registry.register(
        Tool(
            "add_numbers",
            "Add two finite numbers.",
            AddNumbersArguments,
            add_numbers,
            side_effect_free=True,
        )
    )
    return registry
