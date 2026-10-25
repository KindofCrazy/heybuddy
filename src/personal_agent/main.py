import argparse
import asyncio
import logging
import sys
from collections.abc import Callable

from openai import AsyncOpenAI
from pydantic import ValidationError

from personal_agent.config import Settings, load_settings
from personal_agent.core.agent import Agent
from personal_agent.llm.openai_provider import OpenAIProvider
from personal_agent.tools.builtin import create_builtin_registry


async def chat(
    agent: Agent, read: Callable[[str], str] = input, write: Callable[[str], None] = print
) -> None:
    write("Personal Agent V0.1 — 输入 exit 或 quit 退出。")
    while True:
        # The CLI reads synchronously between turns; there are no background tasks in V0.1.
        try:
            user_input = read("你 > ").strip()
        except EOFError:
            break
        if user_input.lower() in {"exit", "quit"}:
            break
        if not user_input:
            continue
        result = await agent.run(user_input)
        if result.status == "completed":
            write(f"Agent > {result.text}")
        else:
            write(f"错误 [{result.status}] > {result.error}")
    write("再见。")


async def run_cli(settings: Settings) -> None:
    # Disable SDK retries: each loop round is exactly one bounded model request.
    async with AsyncOpenAI(
        api_key=settings.api_key.get_secret_value(),
        base_url=str(settings.base_url) if settings.base_url else None,
        timeout=settings.request_timeout_seconds,
        max_retries=0,
    ) as client:
        agent = Agent(
            OpenAIProvider(client, settings.model),
            create_builtin_registry(settings.tool_timeout_seconds),
            max_rounds=settings.max_rounds,
            request_timeout_seconds=settings.request_timeout_seconds,
        )
        await chat(agent)


def main() -> int:
    parser = argparse.ArgumentParser(description="Personal Agent V0.1 CLI")
    parser.parse_args()
    try:
        settings = load_settings()
    except ValidationError as exc:
        # ValidationError.__str__ contains raw input, potentially including the API key.
        fields = ", ".join(sorted({str(error["loc"][0]) for error in exc.errors()}))
        print(f"配置错误：请检查 {fields}。参考 .env.example。", file=sys.stderr)
        return 2
    except OSError:
        print("配置错误：无法读取当前目录的 .env。", file=sys.stderr)
        return 2

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("personal_agent")
    logger.handlers = [handler]
    logger.setLevel(settings.log_level)
    logger.propagate = False
    try:
        asyncio.run(run_cli(settings))
    except (KeyboardInterrupt, EOFError):
        print("\n再见。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
