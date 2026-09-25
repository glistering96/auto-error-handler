from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import sessionmaker

from aeh.config import Settings
from aeh.db import make_session_factory
from apps.review_web.controller import create_router

ROOT = Path(__file__).resolve().parent
TEMPLATES = Environment(
    loader=FileSystemLoader(ROOT / "templates"),
    autoescape=select_autoescape(["html"]),
)


def create_app(settings: Settings | None = None, factory: sessionmaker | None = None) -> FastAPI:
    settings = settings or Settings()
    factory = factory or make_session_factory(settings)
    app = FastAPI(title="Auto Error Handler Review", version="1.0.0")
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    app.include_router(create_router(settings, factory, TEMPLATES))
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("apps.review_web.main:app", host="127.0.0.1", port=8001, reload=False)
