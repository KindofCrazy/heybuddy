"""NapCat lifecycle around the original ChatService and bounded QQ inbox consumer."""

import asyncio
from contextlib import AsyncExitStack

import httpx

from personal_agent.channels.lifecycle import stop_on_termination
from personal_agent.channels.napcat.events import parse_message
from personal_agent.channels.napcat.transport import NapCatTransport
from personal_agent.channels.qq.adapter import QQAdapter
from personal_agent.channels.qq.images import QQImages
from personal_agent.config import Settings
from personal_agent.runtime import open_application
from personal_agent.storage.images import SQLiteImages
from personal_agent.storage.qq import QQInbox


async def run_napcat(
    settings: Settings, *, dashboard: bool = False, dashboard_port: int = 8765
) -> None:
    settings.require_napcat()
    policy = settings.delivery_settings(settings)
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
                    settings.napcat_scope,
                    settings.napcat_peer_id,
                )
                transport = NapCatTransport(settings, policy)
                adapter = QQAdapter(
                    policy,
                    service,
                    inbox,
                    transport,
                    QQImages(policy, client, SQLiteImages(sessions.store, sessions.agent.id)),
                    scope_id=settings.napcat_scope,
                )
                service.qq_status = lambda: {
                    "gateway": transport.state,
                    "consumer": "stopped"
                    if adapter.closing
                    else "running"
                    if adapter.worker and not adapter.worker.done()
                    else "not_running",
                }
                started = asyncio.Event()

                def ready() -> None:
                    if adapter.worker is None:
                        adapter.start()
                        started.set()

                async def receive(payload: dict) -> None:
                    await adapter.accept_message(parse_message(payload, settings, policy))

                receiving = asyncio.create_task(transport.run(receive, ready), name="napcat-socket")
                stopping = asyncio.create_task(stop.wait(), name="napcat-stop")
                starting = asyncio.create_task(started.wait(), name="napcat-ready")
                print("NapCat > 正在核对本机小号身份；只接受授权主号的好友私聊。Ctrl+C 退出。")
                try:
                    await asyncio.wait(
                        {receiving, stopping, starting}, return_when=asyncio.FIRST_COMPLETED
                    )
                    tasks = {receiving, stopping}
                    if adapter.worker is not None:
                        tasks.add(adapter.worker)
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in (receiving, adapter.worker):
                        if task in done:
                            await task
                finally:
                    adapter.closing = True
                    for task in (receiving, stopping, starting):
                        task.cancel()
                    await asyncio.gather(receiving, stopping, starting, return_exceptions=True)
                    await adapter.close()
