from __future__ import annotations

import re
import uuid

from fastapi.responses import JSONResponse

SECRET = re.compile(
    r"(?i)(bearer\s+\S+|sk-[a-z0-9_-]{12,}|(?:password|token|secret|api[_-]?key)\s*[:=]\s*\S+)"
)


def scrub(value: str, limit: int = 2048) -> str:
    return SECRET.sub("[REDACTED]", value)[:limit]


class AehError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = scrub(detail)


def problem(error: AehError, path: str, trace_id: str | None = None) -> JSONResponse:
    body = {
        "type": "urn:aeh:error:" + error.code.lower(),
        "title": "Auto Error Handler request failed",
        "status": error.status,
        "code": error.code,
        "detail": error.detail,
        "instance": path,
        "traceId": trace_id or str(uuid.uuid4()),
    }
    if error.status == 422:
        body["errors"] = [{"path": path, "message": error.detail}]
    return JSONResponse(body, status_code=error.status, media_type="application/problem+json")
