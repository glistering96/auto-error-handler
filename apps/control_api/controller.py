from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import uuid

from codex_cli_bin import bundled_codex_path  # type: ignore[import-untyped]
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from aeh.config import Settings
from aeh.contracts import parse_event
from aeh.db import INCIDENT_STATES, AnalysisRun, Incident, PatchRun
from aeh.errors import AehError
from aeh.service import (
    analysis_response,
    approve,
    incident_detail,
    ingest,
    list_incidents,
    patch_response,
)


def create_router(settings: Settings, factory: sessionmaker) -> APIRouter:
    router = APIRouter()

    @router.get("/health/live")
    def live():
        return {"status": "ok"}

    @router.get("/health/ready")
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

    @router.post("/v1/error-events", status_code=202)
    async def error_events(request: Request, idempotency_key: str | None = Header(default=None)):
        raw = await request.body()
        event, checksum, normalized = parse_event(raw, idempotency_key)
        return await asyncio.to_thread(ingest, settings, factory, event, checksum, normalized)

    @router.get("/v1/incidents")
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

    @router.get("/v1/incidents/{incident_id}")
    def incident(incident_id: uuid.UUID):
        with factory() as db:
            row = db.get(Incident, incident_id)
            if row is None:
                raise AehError(404, "AEH-EVENT-404-001", "Incident not found")
            return incident_detail(db, row)

    @router.get("/v1/incidents/{incident_id}/analysis")
    def analysis(incident_id: uuid.UUID):
        with factory() as db:
            row = db.scalar(select(AnalysisRun).where(AnalysisRun.incident_id == incident_id))
            if row is None:
                raise AehError(404, "AEH-EVENT-404-001", "Analysis not found")
            return analysis_response(db, row)

    @router.get("/v1/incidents/{incident_id}/patch")
    def patch(incident_id: uuid.UUID):
        with factory() as db:
            row = db.scalar(select(PatchRun).where(PatchRun.incident_id == incident_id))
            if row is None:
                raise AehError(404, "AEH-EVENT-404-001", "Patch not found")
            return patch_response(row)

    @router.post("/v1/incidents/{incident_id}/approve", status_code=202)
    async def approval(
        incident_id: uuid.UUID, request: Request, idempotency_key: str | None = Header(default=None)
    ):
        if await request.body():
            raise AehError(400, "AEH-EVENT-400-001", "Approval request must have no body")
        try:
            key = uuid.UUID(idempotency_key or "")
        except ValueError as exc:
            raise AehError(400, "AEH-EVENT-400-001", "Idempotency-Key must be a UUID") from exc
        return await asyncio.to_thread(approve, factory, incident_id, key)

    return router
