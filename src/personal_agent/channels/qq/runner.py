"""QQ lifecycle shares the application's original database, memory and character."""

import asyncio
from contextlib import AsyncExitStack

import httpx

from personal_agent.channels.lifecycle import stop_on_termination
from personal_agent.channels.qq.adapter import QQAdapter
from personal_agent.channels.qq.api import QQAPI
from personal_agent.channels.qq.config import QQError, QQSettings
from personal_agent.channels.qq.events import parse_text
from personal_agent.channels.qq.gateway import QQGateway
from personal_agent.config import Settings
from personal_agent.runtime import open_application
from personal_agent.storage.qq import QQInbox


async def run_qq(
    settings: Settings, *, dashboard: bool = False, dashboard_port: int = 8765
) -> None:
    settings.require_qq()
    stop = asyncio.Event()
    with stop_on_termination(stop):
        async with open_application(settings) as service:
            async with AsyncExitStack() as stack:
                if dashboard:
                    from personal_agent.dashboard.server import dashboard_server

                    await stack.enter_async_context(dashboard_server(service, dashboard_port))
                client = await stack.enter_async_context(
                    httpx.AsyncClient(trust_env=False, follow_redirects=False)
                )
                sessions = service.sessions
                inbox = QQInbox(
                    sessions.store,
                    sessions.agent.id,
                    settings.qq_app_id,
                    settings.qq_allowed_openids[0],
                )
                adapter = QQAdapter(settings, service, inbox, QQAPI(settings, client))
                gateway = QQGateway(settings, adapter.sender)
                service.qq_status = lambda: {
                    "gateway": gateway.state,
                    "consumer": "stopped"
                    if adapter.closing
                    else "running"
                    if adapter.worker and not adapter.worker.done()
                    else "not_running",
                }
                adapter.start()
                receiving = asyncio.create_task(gateway.run(adapter.accept), name="qq-gateway")
                stopping = asyncio.create_task(stop.wait(), name="qq-stop")
                print("QQ > 正在连接官方 Gateway；仅接受授权用户的 C2C 纯文本。Ctrl+C 退出。")
                try:
                    done, _ = await asyncio.wait(
                        {receiving, adapter.worker, stopping}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in (adapter.worker, receiving):
                        if task in done:
                            await task
                finally:
                    adapter.closing = True
                    receiving.cancel()
                    stopping.cancel()
                    # The connection's close handshake has its own three-second timeout.
                    await asyncio.gather(receiving, stopping, return_exceptions=True)
                    await adapter.close()


async def discover_openid(settings: QQSettings) -> None:
    settings.require_qq(discover=True)
    found, stop = asyncio.Event(), asyncio.Event()

    async def receive(payload: dict) -> None:
        message = parse_text(payload, settings)
        if message is not None and not found.is_set():
            # Explicit local diagnostic only. Never print message text or full events.
            print("QQ OpenID > " + message.openid)
            found.set()

    with stop_on_termination(stop):
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
            task = asyncio.create_task(QQGateway(settings, QQAPI(settings, client)).run(receive))
            found_task = asyncio.create_task(found.wait())
            stop_task = asyncio.create_task(stop.wait())
            print("QQ > OpenID 诊断模式：请用测试账号发送一条纯文本；不调用 Agent 或写入会话。")
            try:
                done, _ = await asyncio.wait(
                    {task, found_task, stop_task},
                    timeout=settings.qq_discovery_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if task in done:
                    await task
                if not done:
                    raise QQError("qq_discovery_timeout")
            finally:
                for pending in (task, found_task, stop_task):
                    pending.cancel()
                await asyncio.gather(task, found_task, stop_task, return_exceptions=True)
