import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Callable

from openai import AsyncOpenAI
from pydantic import ValidationError

from personal_agent.config import Settings, load_settings, memory_root_for
from personal_agent.core.agent import Agent
from personal_agent.core.budget import ContextError
from personal_agent.core.history import HistoryError
from personal_agent.core.memory import CoreMemoryError
from personal_agent.llm.openai_provider import OpenAIProvider
from personal_agent.memory import index
from personal_agent.memory.store import MemoryFS
from personal_agent.memory.tools import register_memory_tools
from personal_agent.recall.service import Recall
from personal_agent.recall.tools import register_recall_tools
from personal_agent.reflection.cli import command as reflection_command
from personal_agent.reflection.models import ReflectionError
from personal_agent.reflection.service import ReflectionService
from personal_agent.sessions import SessionManager
from personal_agent.storage.reflection import ReflectionRepository
from personal_agent.storage.sqlite import SQLiteStore
from personal_agent.tools.builtin import create_builtin_registry


async def chat(
    agent: Agent,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
    *,
    sessions: SessionManager | None = None,
    reflection: ReflectionService | None = None,
) -> None:
    write(
        "HeyBuddy V0.7.1 — /new /sessions /resume <id> /status /context status /reflect；"
        "exit 或 quit 退出。"
    )
    write("Deferred 默认可读；/memory edit <请求> 本轮授权写入；/memory check|repair 检查或补链。")
    if sessions is not None:
        write(sessions.status())
    while True:
        # Commands and selection changes occur only between runs.
        try:
            user_input = read("你 > ").strip()
        except EOFError:
            break
        if user_input.lower() in {"exit", "quit"}:
            break
        if not user_input:
            continue
        permissions: frozenset[str] = frozenset()
        if user_input.split(maxsplit=1)[0] == "/memory":
            parts = user_input.split(maxsplit=2)
            if len(parts) == 2 and parts[1] in {"check", "repair"}:
                if sessions is None or not isinstance(sessions.memory, MemoryFS):
                    write("记忆错误 > 当前入口未配置 MemoryFS。")
                    continue
                try:
                    report = (
                        index.repair(sessions.memory)
                        if parts[1] == "repair"
                        else index.inspect(sessions.memory)
                    )
                    write(json.dumps(report, ensure_ascii=False))
                except CoreMemoryError as exc:
                    write(f"记忆错误 [{exc.code}] > {exc}")
                continue
            if len(parts) != 3 or parts[1] not in {"read", "edit"}:
                write(
                    "命令用法：/memory read <请求> | /memory edit <请求> | /memory "
                    "check | /memory repair；History/Restore 已移除。"
                )
                continue
            if sessions is None or sessions.memory is None:
                write("记忆错误 > 当前入口未配置 Core Memory。")
                continue
            permissions = frozenset({"memory.read"})
            if parts[1] == "edit":
                permissions |= {"memory.update", "deferred.update"}
        elif user_input.split(maxsplit=1)[0] == "/reflect":
            if reflection is None or sessions is None:
                write("Reflection 错误 > 当前入口未配置 Reflection。")
                continue
            try:
                await reflection_command(reflection, sessions.current.id, user_input, write)
            except (ReflectionError, CoreMemoryError) as exc:
                write(f"Reflection 错误 [{exc.code}] > 计划未自动批准；请 review/status 核对进度。")
                if isinstance(exc, ReflectionError) and exc.stage:
                    write(
                        json.dumps(
                            {
                                "stage": exc.stage,
                                "provider": exc.diagnostics.model_dump(exclude_none=True)
                                if exc.diagnostics
                                else None,
                            },
                            ensure_ascii=False,
                        )
                    )
            except (HistoryError, ContextError):
                write(
                    "Reflection 存储/历史错误 > 检查 status/review；"
                    "文件写入可能已完成，不会自动重试。"
                )
            continue
        elif user_input == "/context status":
            try:
                write(json.dumps(agent.context.diagnostics(), ensure_ascii=False))
            except (ContextError, HistoryError):
                write("Context 错误 > 无法读取有界上下文状态；请检查数据库与预算配置。")
            continue
        elif user_input.startswith("/"):
            if sessions is None:
                write("错误 > 当前入口未配置持久化会话。")
                continue
            try:
                parts = user_input.split()
                if parts == ["/new"]:
                    sessions.new()
                    agent.context = sessions.context()
                    write(sessions.status())
                elif parts == ["/sessions"]:
                    for conversation in sessions.list():
                        marker = "*" if conversation.id == sessions.current.id else " "
                        write(
                            f"{marker} {conversation.id} | {conversation.title} | "
                            f"{conversation.updated_at.isoformat()}"
                        )
                elif len(parts) == 2 and parts[0] == "/resume":
                    sessions.resume(parts[1])
                    agent.context = sessions.context()
                    write(sessions.status())
                elif parts == ["/status"]:
                    write(sessions.status())
                else:
                    write("命令用法：/new | /sessions | /resume <conversation_id> | /status")
            except HistoryError:
                write("会话错误 > 无法读取或切换会话，请检查 ID 和数据库状态。")
            continue
        result = await agent.run(user_input, permissions=permissions)
        if result.status == "completed":
            write(f"Agent > {result.text}")
        else:
            write(f"错误 [{result.status}] > {result.error}")
    write("再见。")


async def run_cli(settings: Settings) -> None:
    with SQLiteStore(settings.database_path) as store:
        store.initialize()
        recovered = store.recover_incomplete_runs()
        if recovered:
            print(f"发现 {recovered} 个未完成 Run，已标记 interrupted；不会重试工具。")
        sessions = SessionManager(store, budget=settings.context_budget())
        root = memory_root_for(settings, sessions.agent.id)
        memory = MemoryFS.for_agent(root, sessions.agent.id, max_bytes=settings.memory_max_bytes)
        memory.initialize()
        sessions.memory = memory
        await _run_session(settings, sessions, memory)


async def _run_session(settings: Settings, sessions: SessionManager, memory: MemoryFS) -> None:
    tools = create_builtin_registry(settings.tool_timeout_seconds)
    register_memory_tools(tools, memory)
    register_recall_tools(tools, Recall(sessions.store, sessions.agent.id))
    # Disable SDK retries: each loop round is exactly one bounded model request.
    async with AsyncOpenAI(
        api_key=settings.api_key.get_secret_value(),
        base_url=str(settings.base_url) if settings.base_url else None,
        timeout=settings.request_timeout_seconds,
        max_retries=0,
    ) as client:
        agent = Agent(
            OpenAIProvider(client, settings.model),
            tools,
            context=sessions.context(),
            max_rounds=settings.max_rounds,
            request_timeout_seconds=settings.request_timeout_seconds,
        )
        reflection = ReflectionService(
            ReflectionRepository(sessions.store, sessions.agent.id),
            memory,
            agent.provider,
            settings.context_budget(),
            settings.reflection_policy(),
        )
        await chat(agent, sessions=sessions, reflection=reflection)


def main() -> int:
    parser = argparse.ArgumentParser(description="Personal Agent / HeyBuddy V0.7.1 CLI")
    parser.parse_args()
    try:
        settings = load_settings()
    except ValidationError as exc:
        # ValidationError.__str__ contains raw input, potentially including the API key.
        fields = ", ".join(
            sorted(
                {
                    (str(error["loc"][0]) if error["loc"] else "context_budget")
                    for error in exc.errors()
                }
            )
        )
        print(f"配置错误：请检查 {fields}。参考 .env.example。", file=sys.stderr)
        return 2
    except OSError:
        print("配置错误：无法读取当前目录的 .env。", file=sys.stderr)
        return 2

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger = logging.getLogger("personal_agent")
    previous_logging = (logger.handlers[:], logger.level, logger.propagate)
    logger.handlers = [handler]
    logger.setLevel(settings.log_level)
    logger.propagate = False
    try:
        asyncio.run(run_cli(settings))
    except (KeyboardInterrupt, EOFError):
        print("\n再见。")
    except HistoryError:
        print(
            "存储错误：无法初始化或访问会话数据库，请检查权限或其他运行中的实例。", file=sys.stderr
        )
        return 1
    except CoreMemoryError as exc:
        if exc.code in {"memory_not_private", "memory_privacy_error"}:
            print(
                f"记忆错误 [{exc.code}]：目录须位于 Git 仓库外，或整目录被忽略且无已跟踪文件；"
                "请检查忽略规则、Git 可用性与索引，不会自动修改 Git 配置。",
                file=sys.stderr,
            )
        else:
            print(f"记忆错误 [{exc.code}]：请检查记忆目录、完整性和容量配置。", file=sys.stderr)
        return 1
    finally:
        logger.handlers, previous_level, logger.propagate = previous_logging
        logger.setLevel(previous_level)
        handler.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
