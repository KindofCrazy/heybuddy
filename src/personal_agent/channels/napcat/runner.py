"""NapCat lifecycle around the original ChatService and bounded QQ inbox consumer."""

import asyncio
from contextlib import AsyncExitStack

import httpx

from personal_agent.channels.lifecycle import stop_on_termination
from personal_agent.channels.napcat.events import parse_input_status, parse_message
from personal_agent.channels.napcat.proactive import NapCatProactiveSender
from personal_agent.channels.napcat.reminders import NapCatReminderSender
from personal_agent.channels.napcat.transport import NapCatTransport
from personal_agent.channels.qq.adapter import QQAdapter
from personal_agent.channels.qq.humanize import Humanizer
from personal_agent.channels.qq.images import QQImages
from personal_agent.config import Settings
from personal_agent.proactive.delivery import DeliveryGrant
from personal_agent.runtime import open_application
from personal_agent.scheduler.chat import source_scope
from personal_agent.scheduler.models import ReminderGrant
from personal_agent.storage.images import SQLiteImages
from personal_agent.storage.outbox import OutboundContext
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
                reminder_sender = NapCatReminderSender(transport)
                adapter = QQAdapter(
                    policy,
                    service,
                    inbox,
                    transport,
                    QQImages(policy, client, SQLiteImages(sessions.store, sessions.agent.id)),
                    scope_id=settings.napcat_scope,
                    humanizer=Humanizer(policy, length_based_gaps=True),
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
                outbound = (
                    service.organization.proactive.outbound
                    if service.organization and service.organization.proactive
                    else None
                )
                if outbound is not None:
                    sessions.notice_factory = lambda conversation: OutboundContext(
                        outbound.repository, conversation, source_scope=source_scope(inbox.scope)
                    )
                    service.agent.context.notices = sessions.notice_factory(sessions.current.id)
                connection_revision = 0

                def proactive_grant() -> DeliveryGrant | None:
                    if (
                        not settings.napcat_proactive_enabled
                        or transport.state != "ready"
                        or adapter.closing
                        or inbox.pending()
                    ):
                        return None
                    return DeliveryGrant(
                        enabled=True,
                        revision=connection_revision,
                        channel="napcat",
                        conversation_id=adapter.conversation_id,
                        recipient=settings.napcat_peer_id,
                        source_scope=source_scope(inbox.scope),
                        channel_scope=settings.napcat_scope,
                    )

                def reminder_grant() -> ReminderGrant | None:
                    if not settings.napcat_reminders_enabled or transport.state != "ready":
                        return None
                    return ReminderGrant(
                        enabled=True,
                        channel="napcat",
                        conversation_id=adapter.conversation_id,
                        recipient=settings.napcat_peer_id,
                        source_scope=source_scope(inbox.scope),
                    )

                def ready() -> None:
                    nonlocal connection_revision
                    connection_revision += 1
                    if service.scheduler is not None and settings.napcat_reminders_enabled:
                        service.scheduler.bind_delivery(reminder_sender, reminder_grant)
                    if outbound is not None and settings.napcat_proactive_enabled:
                        outbound.bind_delivery(
                            NapCatProactiveSender(transport, outbound.delivery_allowed),
                            proactive_grant,
                        )
                    if adapter.worker is None:
                        adapter.start()
                        started.set()

                last_status_stamp = 0

                async def receive(payload: dict) -> None:
                    nonlocal last_status_stamp
                    status = parse_input_status(payload, settings)
                    if status is not None:
                        stamp, until = status
                        if stamp >= last_status_stamp:
                            last_status_stamp = stamp
                            adapter.note_typing(until)
                        return
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
                    if outbound is not None:
                        outbound.unbind_delivery()
                    if service.scheduler is not None:
                        service.scheduler.unbind_delivery()
                    adapter.closing = True
                    for task in (receiving, stopping, starting):
                        task.cancel()
                    await asyncio.gather(receiving, stopping, starting, return_exceptions=True)
                    await adapter.close()
