from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from contextlib import asynccontextmanager

from codex_cli_bin import bundled_codex_path  # type: ignore[import-untyped]
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from aeh.config import Settings
from aeh.contracts import parse_event
from aeh.db import INCIDENT_STATES, AnalysisRun, Incident, PatchRun, make_session_factory
from aeh.errors import AehError, problem
from aeh.service import (
    analysis_response,
    approve,
    incident_detail,
    ingest,
    list_incidents,
    patch_response,
    sync_services,
)


def create_app(settings: Settings | None = None, factory: sessionmaker | None = None) -> FastAPI:
    settings = settings or Settings()
    factory = factory or make_session_factory(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sync_services(settings, factory)
        yield

    app = FastAPI(title="Auto Error Handler MVP", version="1.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def trace(request: Request, call_next):
        request.state.trace_id = str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response

    @app.exception_handler(AehError)
    async def aeh_error(request: Request, error: AehError):
        return problem(error, request.url.path, request.state.trace_id)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return problem(
            AehError(422, "AEH-EVENT-422-001", "Request parameter is invalid"),
            request.url.path,
            request.state.trace_id,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, error: Exception):
        return problem(
            AehError(500, "AEH-INTERNAL-500-001", "Internal error"),
            request.url.path,
            request.state.trace_id,
        )

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        try:
            with factory() as db:
                db.execute(text("SELECT 1"))
            settings.load_services()
            if not settings.use_fake_codex:
                if not shutil.which("bwrap"):
                    raise RuntimeError("network isolation unavailable")
                if not bundled_codex_path().is_file():
                    raise RuntimeError("Codex runtime unavailable")
                if settings.codex_auth_mode == "api-key" and not os.environ.get("OPENAI_API_KEY"):
                    raise RuntimeError("OPENAI_API_KEY unavailable")
                if settings.codex_auth_mode == "local":
                    status = subprocess.run(
                        ["codex", "login", "status"], capture_output=True, timeout=5, check=False
                    )
                    if status.returncode:
                        raise RuntimeError("Codex login unavailable")
        except (OSError, RuntimeError, ValueError, TypeError, TimeoutError):
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return {"status": "ready"}

    @app.post("/v1/error-events", status_code=202)
    async def error_events(request: Request, idempotency_key: str | None = Header(default=None)):
        raw = await request.body()
        event, checksum, normalized = parse_event(raw, idempotency_key)
        return ingest(settings, factory, event, checksum, normalized)

    @app.get("/v1/incidents")
    def incidents(
        serviceKey: str | None = None,
        state: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ):
        if state and state not in INCIDENT_STATES:
            raise AehError(400, "AEH-EVENT-400-001", "Unknown incident state")
        with factory() as db:
            return list_incidents(settings, db, serviceKey, state, cursor, limit)

    @app.get("/v1/incidents/{incident_id}")
    def incident(incident_id: uuid.UUID):
        with factory() as db:
            row = db.get(Incident, incident_id)
            if row is None:
                raise AehError(404, "AEH-EVENT-404-001", "Incident not found")
            return incident_detail(db, row)

    @app.get("/v1/incidents/{incident_id}/analysis")
    def analysis(incident_id: uuid.UUID):
        with factory() as db:
            row = db.scalar(select(AnalysisRun).where(AnalysisRun.incident_id == incident_id))
            if row is None:
                raise AehError(404, "AEH-EVENT-404-001", "Analysis not found")
            return analysis_response(db, row)

    @app.get("/v1/incidents/{incident_id}/patch")
    def patch(incident_id: uuid.UUID):
        with factory() as db:
            row = db.scalar(select(PatchRun).where(PatchRun.incident_id == incident_id))
            if row is None:
                raise AehError(404, "AEH-EVENT-404-001", "Patch not found")
            return patch_response(row)

    @app.post("/v1/incidents/{incident_id}/approve", status_code=202)
    async def approval(
        incident_id: uuid.UUID, request: Request, idempotency_key: str | None = Header(default=None)
    ):
        if await request.body():
            raise AehError(400, "AEH-EVENT-400-001", "Approval request must have no body")
        try:
            key = uuid.UUID(idempotency_key or "")
        except ValueError as exc:
            raise AehError(400, "AEH-EVENT-400-001", "Idempotency-Key must be a UUID") from exc
        return approve(factory, incident_id, key)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("apps.control_api.main:app", host="127.0.0.1", port=8000, reload=False)
