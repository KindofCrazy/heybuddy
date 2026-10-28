"""HTTP views of a supplied ChatService; never opens or owns another application."""

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from personal_agent.core.history import HistoryError
from personal_agent.core.memory import CoreMemoryError
from personal_agent.dashboard.activity import activity
from personal_agent.dashboard.layout import CARDS
from personal_agent.dashboard.memory import MemoryView, markdown
from personal_agent.dashboard.privacy import safe
from personal_agent.dashboard.repository import NotFound, ObservatoryRepository
from personal_agent.runtime import ChatService

ASSETS = Path(__file__).parent


def create_app(service: ChatService, *, port: int = 8765) -> FastAPI:
    if service.sessions is None:
        raise ValueError("Observatory requires the existing persistent ChatService")
    sessions = service.sessions
    repository = ObservatoryRepository(sessions.store, sessions.agent.id)
    memory = MemoryView(sessions.memory)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    app.state.repository = repository
    templates = Jinja2Templates(directory=ASSETS / "templates")

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        # Reject DNS rebinding, cross-site reads and all mutation methods.
        host = request.headers.get("host", "")
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origin = request.headers.get("origin")
        if host not in allowed or (origin and origin not in {"http://" + h for h in allowed}):
            return JSONResponse({"error": "local_access_only"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"error": "local_access_only"}, status_code=403)
        if request.method not in {"GET", "HEAD"}:
            return JSONResponse({"error": "read_only"}, status_code=405)
        try:
            response = await call_next(request)
        except Exception:
            # No exception body, URL parameters, message text or credentials in logs.
            response = JSONResponse({"error": "observatory_unavailable"}, status_code=503)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                "style-src 'self'; "
                "img-src 'none'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'none'",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cross-Origin-Resource-Policy": "same-origin",
            }
        )
        return response

    @app.exception_handler(NotFound)
    async def missing(request, exc):
        return JSONResponse({"error": "not_found"}, status_code=404)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return JSONResponse({"error": "invalid_request"}, status_code=422)

    @app.exception_handler(HistoryError)
    async def storage_error(request, exc):
        return JSONResponse({"error": "storage_unavailable"}, status_code=503)

    @app.exception_handler(CoreMemoryError)
    async def memory_error(request, exc):
        return JSONResponse({"error": "memory_unavailable"}, status_code=404)

    def output(value):
        data = safe(value)
        if len(json.dumps(data, ensure_ascii=False).encode()) > 524288:
            return JSONResponse({"error": "response_budget"}, status_code=503)
        return JSONResponse(data)

    @app.get("/")
    async def home(request: Request):
        return templates.TemplateResponse(
            request=request, name="index.html", context={"cards": CARDS}
        )

    @app.get("/api/status")
    async def status():
        recent = repository.listing("runs", 1)["items"]
        org = service.organization
        stage = org.current_stage if org else None
        diagnostics = org.diagnostics(sessions.current.id, read_only=True) if org else None
        return output(
            {
                "state": "Running"
                if service.foreground_active
                else "Maintaining"
                if stage
                else "Unknown"
                if service.observer_only and recent and recent[0]["status"] == "running"
                else "Idle",
                "current_stage": stage,
                "latest_run": recent[0] if recent else None,
                "maintenance_running": stage is not None,
                "foreground_active": service.foreground_active,
                "observer_only": service.observer_only,
                "schedule": repository.schedule(),
                "diagnostics": diagnostics,
                "qq": service.qq_status() if service.qq_status else {"gateway": "not_attached"},
                "read_only": True,
            }
        )

    @app.get("/api/activity")
    async def recent_activity(
        limit: int = Query(12, ge=1, le=30), cursor: str = Query("", max_length=512)
    ):
        try:
            return output(activity(repository, service, limit, cursor))
        except ValueError:
            return JSONResponse({"error": "invalid_cursor"}, status_code=422)

    # Async handlers keep SQLite on its owning event-loop thread.
    @app.get("/api/runs")
    async def runs(
        limit: int = Query(8, ge=1, le=20),
        cursor: int = Query(2**63 - 1, ge=1, le=2**63 - 1),
        episode_id: str | None = Query(None, max_length=128),
    ):
        return output(repository.listing("runs", limit, cursor, episode_id=episode_id))

    @app.get("/api/runs/{run_id}")
    async def run(run_id: str):
        return output(repository.detail("runs", run_id))

    @app.get("/api/runs/{run_id}/messages")
    async def messages(
        run_id: str, limit: int = Query(8, ge=1, le=12), after: int = Query(0, ge=0, le=2**63 - 1)
    ):
        return output(repository.messages(run_id, limit, after))

    @app.get("/api/episodes")
    async def episodes(
        limit: int = Query(8, ge=1, le=20),
        cursor: int = Query(2**63 - 1, ge=1, le=2**63 - 1),
        thread_id: str | None = Query(None, max_length=128),
    ):
        return output(repository.listing("episodes", limit, cursor, thread_id=thread_id))

    @app.get("/api/episodes/{episode_id}")
    async def episode(episode_id: str):
        return output(repository.detail("episodes", episode_id))

    @app.get("/api/threads")
    async def threads(
        limit: int = Query(8, ge=1, le=20), cursor: int = Query(2**63 - 1, ge=1, le=2**63 - 1)
    ):
        return output(repository.listing("threads", limit, cursor))

    @app.get("/api/threads/{thread_id}")
    async def thread(thread_id: str):
        return output(repository.detail("threads", thread_id))

    @app.get("/api/learning/jobs")
    async def jobs(
        limit: int = Query(8, ge=1, le=20), cursor: int = Query(2**63 - 1, ge=1, le=2**63 - 1)
    ):
        return output(repository.listing("jobs", limit, cursor))

    @app.get("/api/learning/jobs/{job_id}")
    async def job(job_id: str):
        return output(repository.detail("jobs", job_id))

    @app.get("/api/memory")
    async def memory_tree(
        directory: str = Query("", max_length=260),
        limit: int = Query(8, ge=1, le=20),
        cursor: str = Query("", max_length=128),
    ):
        return output(await asyncio.to_thread(memory.listing, directory, limit, cursor))

    @app.get("/api/memory/file")
    async def memory_file(
        path: str = Query(..., min_length=1, max_length=260),
        offset: int = Query(0, ge=0, le=1048576),
        expected_hash: str | None = Query(None, max_length=64),
    ):
        return output(await asyncio.to_thread(memory.file, path, offset, expected_hash))

    @app.get("/api/memory/records")
    async def memory_records(
        path: str = Query(..., min_length=1, max_length=260),
        limit: int = Query(8, ge=1, le=20),
        cursor: int = Query(2**63 - 1, ge=1, le=2**63 - 1),
    ):
        memory.memory.path(path)
        return output(repository.file_jobs(path, limit, cursor))

    @app.get("/api/character")
    async def character():
        compiler = sessions.character
        if compiler is None:
            return output({"enabled": False, "message": "No active character package"})
        package = compiler.package
        return output(
            {
                "enabled": True,
                "character_id": package.character_id,
                "fingerprint": package.fingerprint,
                "profile_html": markdown(package.character),
                "behavior_html": markdown(package.behavior),
                "profile": package.character,
                "behavior": package.behavior,
                "state_available": bool(
                    service.organization and service.organization.character_state
                ),
            }
        )

    @app.get("/api/character/state")
    async def character_state(
        limit: int = Query(8, ge=1, le=20),
        cursor: int = Query(2**63 - 1, ge=1, le=2**63 - 1),
    ):
        actor = service.organization.character_state if service.organization else None
        if actor is None:
            return output(
                {
                    "enabled": False,
                    "active": None,
                    "stored": None,
                    "items": [],
                    "next_cursor": None,
                    "has_more": False,
                }
            )
        stored = actor.repository.current() if actor.repository.exists() else None
        return output(
            {
                "enabled": actor.policy.enabled,
                "active": actor.active(),
                "stored": stored.model_dump(mode="json") if stored else None,
                "history_note": "Only the current state and source-attempt receipts are retained; "
                "previous state bodies are unavailable. "
                "State is simulated, not subjective emotion.",
                **repository.character_receipts(actor.repository.scope, limit, cursor),
            }
        )

    app.mount("/static", StaticFiles(directory=ASSETS / "static"), name="static")
    return app
