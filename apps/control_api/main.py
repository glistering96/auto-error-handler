from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import sessionmaker

from aeh.config import Settings
from aeh.db import make_session_factory
from aeh.errors import AehError, problem
from apps.control_api.controller import create_router


def create_app(settings: Settings | None = None, factory: sessionmaker | None = None) -> FastAPI:
    settings = settings or Settings()
    factory = factory or make_session_factory(settings)
    app = FastAPI(title="Auto Error Handler MVP", version="1.0.0")

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

    app.include_router(create_router(settings, factory))
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("apps.control_api.main:app", host="127.0.0.1", port=8000, reload=False)
