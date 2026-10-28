import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Callable

from pydantic import ValidationError

from personal_agent.automation.service import MemoryAutomation
from personal_agent.channels.qq.config import QQError
from personal_agent.channels.qq.runner import discover_openid, run_qq
from personal_agent.character.models import CharacterError
from personal_agent.cli_commands import MaintenanceCommands
from personal_agent.config import Settings, load_qq_settings, load_settings
from personal_agent.console import AsyncInput
from personal_agent.core.agent import Agent
from personal_agent.core.budget import ContextError
from personal_agent.core.history import HistoryError
from personal_agent.core.memory import CoreMemoryError
from personal_agent.memory import index
from personal_agent.memory.store import MemoryFS
from personal_agent.organization.models import OrganizationError
from personal_agent.organization.runtime import OrganizationRuntime
from personal_agent.reflection.cli import command as reflection_command
from personal_agent.reflection.models import ReflectionError
from personal_agent.reflection.service import ReflectionService
from personal_agent.runtime import ChatService, open_application, open_chat
from personal_agent.sessions import SessionManager


async def chat(
    agent: Agent | ChatService,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
    *,
    sessions: SessionManager | None = None,
    reflection: ReflectionService | None = None,
    automation: MemoryAutomation | None = None,
    organization: OrganizationRuntime | None = None,
) -> None:
    # Embedding callers may pass separate parts; production passes the exact owned service.
    owns_service = not isinstance(agent, ChatService)
    service = (
        ChatService(agent, sessions, reflection, automation, organization)
        if owns_service
        else agent
    )
    organization, sessions = service.organization, service.sessions
    console = AsyncInput() if organization else None
    try:
        if organization is not None and sessions is not None:
            organization.schedule(sessions.current.id)
        await _chat(service, read, write, console)
    finally:
        if console is not None:
            console.close()
        if owns_service:
            await service.close()


async def _chat(
    service: ChatService,
    read: Callable[[str], str],
    write: Callable[[str], None],
    console: AsyncInput | None,
) -> None:
    agent, sessions = service.agent, service.sessions
    reflection, automation, organization = (
        service.reflection,
        service.automation,
        service.organization,
    )
    commands = MaintenanceCommands(organization) if organization else None
    write(
        "HeyBuddy 持续对话 — 自动恢复上次对话流；/status /context status /reflect；"
        "exit 或 quit 退出。"
    )
    write("Deferred 默认可读；/memory edit <请求> 本轮授权写入；/memory check|repair 检查或补链。")
    if sessions is not None:
        write(sessions.status())
    while True:
        if organization is not None and sessions is not None and organization.task is None:
            organization.schedule(sessions.current.id)
        # Commands and selection changes occur only between runs.
        try:
            user_input = (await console.read(read, "你 > ") if console else read("你 > ")).strip()
        except EOFError:
            break
        if user_input.lower() in {"exit", "quit"}:
            break
        if organization is not None:
            await organization.foreground()
        if not user_input:
            continue
        permissions: frozenset[str] = frozenset()
        if user_input.split(maxsplit=1)[0] == "/character":
            if organization is None or organization.character_state is None:
                write("Character > 当前未启用角色状态。")
                continue
            try:
                report = await commands.character_command(
                    user_input.removeprefix("/character").strip()
                )
                write(json.dumps(report, ensure_ascii=False))
            except (HistoryError, OrganizationError):
                write("Character > 状态维护失败；原始对话保留，不重试工具。")
            continue
        if user_input.split(maxsplit=1)[0] in {"/episode", "/thread"}:
            if organization is None or sessions is None:
                write("Episode > 当前入口未配置 Organizer。")
                continue
            try:
                await commands.command(sessions.current.id, user_input, write)
            except (HistoryError, OrganizationError):
                write("Episode > 整理/存储失败，原始 Run 已保留；请检查 status。")
            continue
        elif user_input.split(maxsplit=1)[0] == "/memory":
            parts = user_input.split(maxsplit=2)
            if len(parts) == 3 and parts[1] in {"learning", "curator"}:
                if organization is None or organization.learning is None:
                    write("Memory > 当前入口未配置 P2 Learning。")
                    continue
                try:
                    report = await commands.learning_command(
                        parts[2], curator=parts[1] == "curator"
                    )
                    write(json.dumps(report, ensure_ascii=False))
                except (HistoryError, CoreMemoryError, ReflectionError, OrganizationError):
                    write("Memory > 学习失败；检查状态，文件可能已保存，不会盲目重写。")
                continue
            if len(parts) == 3 and parts[1] == "automation":
                if organization and organization.learning and organization.learning.policy.enabled:
                    write(
                        "Memory > P2 已接管自动学习；旧 Automation 已暂停，旧计划可 /reflect 查看。"
                    )
                    continue
                if automation is None or sessions is None:
                    write("Memory > 当前入口未配置 Automation。")
                    continue
                if parts[2] in {"off", "suggest", "auto"}:
                    automation.set_mode(parts[2])
                    write(f"Memory > automation={parts[2]}；仅当前进程生效。")
                elif parts[2] == "status":
                    write(json.dumps(automation.status(sessions.current.id), ensure_ascii=False))
                elif parts[2] in {"run", "retry"}:
                    write("Memory > 正在检查未处理历史；普通回答已完成，可 Ctrl+C 退出。")
                    report = await automation.run(sessions.current.id, retry=parts[2] == "retry")
                    write("Memory > " + json.dumps(report, ensure_ascii=False))
                else:
                    write("用法：/memory automation off|suggest|auto|status|run|retry")
                continue
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
        result = await service.run(user_input, permissions=permissions)
        if result.status == "completed":
            write(f"Agent > {result.text}")
        else:
            write(f"错误 [{result.status}] > {result.error}")
        if (
            automation is not None
            and organization is None
            and sessions is not None
            and automation.policy.mode != "off"
            and result.status == "completed"
        ):
            write(
                "Memory > 后续记忆检查（"
                + automation.policy.mode
                + "）；回答已完成，最多四次额外请求。"
            )
            try:
                report = await automation.run(sessions.current.id)
                write("Memory > " + json.dumps(report, ensure_ascii=False))
            except (HistoryError, CoreMemoryError):
                write("Memory > 检查存储失败；普通回答已保存，不会自动重试。")
        service.after_reply(result)
    write("再见。")


async def run_cli(settings: Settings) -> None:
    async with open_application(settings) as service:
        await chat(service)


async def _run_session(settings: Settings, sessions: SessionManager, memory: MemoryFS) -> None:
    async with open_chat(settings, sessions, memory) as service:
        await chat(service)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Personal Agent / HeyBuddy CLI and official QQ C2C"
    )
    parser.add_argument("channel", nargs="?", choices=("chat", "qq", "web"), default="chat")
    parser.add_argument("--dashboard", action="store_true", help="QQ: enable local Observatory")
    parser.add_argument("--dashboard-port", type=int, default=8765, help="Loopback HTTP port")
    parser.add_argument(
        "--discover-openid",
        action="store_true",
        help="QQ only: receive one test OpenID without Agent, database or model calls",
    )
    args = parser.parse_args()
    if args.discover_openid and args.channel != "qq":
        parser.error("--discover-openid requires qq")
    if args.dashboard and (args.channel != "qq" or args.discover_openid):
        parser.error("--dashboard requires qq without --discover-openid")
    if not 1024 <= args.dashboard_port <= 65535:
        parser.error("--dashboard-port must be between 1024 and 65535")
    try:
        settings = load_qq_settings() if args.discover_openid else load_settings()
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
    logger.setLevel(getattr(settings, "log_level", "INFO"))
    logger.propagate = False
    try:
        if args.discover_openid:
            asyncio.run(discover_openid(settings))
        elif args.channel == "qq":
            if args.dashboard:
                asyncio.run(run_qq(settings, dashboard=True, dashboard_port=args.dashboard_port))
            else:
                asyncio.run(run_qq(settings))
        elif args.channel == "web":
            from personal_agent.dashboard.server import run_web

            asyncio.run(run_web(settings, args.dashboard_port))
        else:
            asyncio.run(run_cli(settings))
    except (KeyboardInterrupt, EOFError):
        print("\n再见。")
    except QQError as exc:
        print(
            f"QQ 错误 [{exc.code}]：检查本地 QQ 配置、平台权限、出口 IP 和网络；"
            "不会重跑已领取的消息或不确定发送。",
            file=sys.stderr,
        )
        return 2 if exc.code in {"qq_configuration", "qq_openid_required"} else 1
    except HistoryError:
        print(
            "存储错误：无法初始化或访问会话数据库，请检查权限或其他运行中的实例。", file=sys.stderr
        )
        return 1
    except OSError:
        print("本地服务无法启动：请检查监听端口和文件权限。", file=sys.stderr)
        return 1
    except CharacterError as exc:
        print(f"角色配置错误 [{exc.code}]：请检查本地角色包与大小限制。", file=sys.stderr)
        return 2
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
