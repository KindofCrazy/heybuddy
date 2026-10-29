from datetime import UTC, datetime

from personal_agent.core.temporal import temporal_context
from personal_agent.tools.registry import Tool, ToolArguments, ToolRegistry


class CurrentTimeArguments(ToolArguments):
    pass


class AddNumbersArguments(ToolArguments):
    a: float
    b: float


def get_current_time(_: CurrentTimeArguments) -> dict[str, str]:
    return {"time": datetime.now(UTC).isoformat(), "timezone": "UTC"}


def get_local_time(_: CurrentTimeArguments) -> dict[str, str]:
    return {"time": temporal_context(None)["current_time"]}


def add_numbers(arguments: AddNumbersArguments) -> dict[str, float]:
    return {"sum": arguments.a + arguments.b}


def create_builtin_registry(
    timeout_seconds: float = 10, *, include_add_numbers: bool = True, local_time: bool = False
) -> ToolRegistry:
    registry = ToolRegistry(timeout_seconds)
    registry.register(
        Tool(
            "get_current_time",
            (
                "获取与当前上下文一致的宿主本地日期和时间；无参数。"
                "上下文时间已足够时直接使用；需要刷新时再调用。普通问时间直接回答本地时间。"
                if local_time
                else (
                    "问题需要当前时间时获取 UTC 日期和时间；无参数。"
                    "不用于与当前时间无关的概念问题。"
                )
            ),
            CurrentTimeArguments,
            get_local_time if local_time else get_current_time,
            side_effect_free=True,
        )
    )
    # Preserve the small arithmetic fixture for existing offline harness evaluations.
    if include_add_numbers:
        registry.register(
            Tool(
                "add_numbers",
                ("需要核对算术时，将有限数字 a 和 b 相加并返回 sum；不用于非数值问题。"),
                AddNumbersArguments,
                add_numbers,
                side_effect_free=True,
            )
        )
    return registry
