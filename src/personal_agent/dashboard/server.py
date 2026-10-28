"""One optional HTTP task beside the original runtime, on loopback only."""

import asyncio
import logging
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext

import uvicorn

from personal_agent.config import Settings
from personal_agent.dashboard.app import create_app
from personal_agent.runtime import ChatService, open_application

logger = logging.getLogger(__name__)


class LocalServer(uvicorn.Server):
    def capture_signals(self):
        # The CLI / QQ runner owns Ctrl+C and SIGTERM for the whole process.
        return nullcontext()


@asynccontextmanager
async def dashboard_server(service: ChatService, port: int = 8765) -> AsyncIterator[None]:
    server = LocalServer(
        uvicorn.Config(
            create_app(service, port=port),
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_config=None,
            log_level="critical",
            server_header=False,
            proxy_headers=False,
            timeout_graceful_shutdown=3,
            limit_concurrency=16,
        )
    )
    # Pre-bind so a port conflict is handled before starting the shared consumers.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", port))
        listener.setblocking(False)

        async def serve():
            try:
                await server.serve(sockets=[listener])
            except Exception:
                logger.error("dashboard_server_unavailable")

        task = asyncio.create_task(serve(), name="observatory-http")
        try:
            for _ in range(100):
                if server.started:
                    break
                if task.done():
                    raise OSError("Dashboard startup failed")
                await asyncio.sleep(0.01)
            if not server.started:
                raise OSError("Dashboard startup timeout")
            print(f"Observatory > http://127.0.0.1:{port} (read-only)")
            yield
        finally:
            server.should_exit = True
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def run_web(settings: Settings, port: int = 8765) -> None:
    from personal_agent.channels.lifecycle import stop_on_termination

    stop = asyncio.Event()
    with stop_on_termination(stop):
        async with open_application(settings, observer_only=True) as service:
            # Read-only standalone mode does not schedule maintenance or call the model.
            async with dashboard_server(service, port):
                await stop.wait()
