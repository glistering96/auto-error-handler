from __future__ import annotations

import uuid

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment
from sqlalchemy.orm import sessionmaker

from aeh.config import Settings
from aeh.errors import AehError
from aeh.review_service import ReviewService


def create_router(settings: Settings, factory: sessionmaker, templates: Environment) -> APIRouter:
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    def home():
        return RedirectResponse("/review/incidents", status_code=307)

    @router.get("/review/incidents", response_class=HTMLResponse)
    def incidents(
        serviceKey: str | None = None,
        state: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ):
        try:
            with factory() as db:
                page = ReviewService(settings, db).list_page(serviceKey, state, cursor, limit)
            return HTMLResponse(templates.get_template("list.html").render(**page))
        except AehError as exc:
            return HTMLResponse(
                templates.get_template("error.html").render(detail=exc.detail),
                status_code=exc.status,
            )

    @router.get("/review/incidents/{incident_id}", response_class=HTMLResponse)
    def incident(incident_id: uuid.UUID):
        try:
            with factory() as db:
                page = ReviewService(settings, db).detail_page(incident_id)
            return HTMLResponse(templates.get_template("detail.html").render(**page))
        except AehError as exc:
            return HTMLResponse(
                templates.get_template("error.html").render(detail=exc.detail),
                status_code=exc.status,
            )

    return router
