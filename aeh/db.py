from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    create_engine,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from aeh.config import Settings


def now() -> datetime:
    return datetime.now(UTC)


def new_id() -> uuid.UUID:
    return uuid.uuid4()


class UtcDateTime(TypeDecorator[datetime]):
    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(DateTime(timezone=dialect.name == "postgresql"))

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        utc = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return utc.replace(tzinfo=None) if dialect.name == "sqlite" else utc

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), server_default=func.now())


class Updated:
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), server_default=func.now(), onupdate=func.now()
    )


class Service(Base, Timestamped, Updated):
    __tablename__ = "services"
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=new_id)
    key: Mapped[str] = mapped_column(String(100), unique=True)
    repository_path: Mapped[str] = mapped_column(Text)
    default_branch: Mapped[str] = mapped_column(String(255))
    policy: Mapped[dict] = mapped_column(DOCUMENT)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class ErrorEvent(Base, Timestamped):
    __tablename__ = "error_events"
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=new_id)
    service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("services.id"))
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True))
    payload_checksum: Mapped[str] = mapped_column(String(64))
    normalized_payload: Mapped[dict] = mapped_column(DOCUMENT)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=now)
    __table_args__ = (Index("uq_error_event_identity", "service_id", "event_id", unique=True),)


OPEN_STATES = ("RECEIVED", "ANALYZING", "AWAITING_APPROVAL", "PATCHING", "VALIDATING")
INCIDENT_STATES = OPEN_STATES + ("ANALYSIS_FAILED", "PATCH_READY", "PATCH_FAILED")


class Incident(Base, Timestamped, Updated):
    __tablename__ = "incidents"
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=new_id)
    service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("services.id"))
    fingerprint: Mapped[str] = mapped_column(String(200))
    state: Mapped[str] = mapped_column(String(32), default="RECEIVED")
    version: Mapped[int] = mapped_column(Integer, default=1)
    repository_path_snapshot: Mapped[str] = mapped_column(Text)
    base_commit_sha: Mapped[str] = mapped_column(String(64))
    first_error_event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("error_events.id"))
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        CheckConstraint(
            "state IN ('RECEIVED','ANALYZING','AWAITING_APPROVAL','ANALYSIS_FAILED','PATCHING','VALIDATING','PATCH_READY','PATCH_FAILED')"
        ),
        CheckConstraint("occurrence_count >= 1"),
        Index(
            "uq_incidents_open_grouping",
            "service_id",
            "fingerprint",
            "base_commit_sha",
            unique=True,
            postgresql_where=text(
                "state IN ('RECEIVED','ANALYZING','AWAITING_APPROVAL','PATCHING','VALIDATING')"
            ),
            sqlite_where=text(
                "state IN ('RECEIVED','ANALYZING','AWAITING_APPROVAL','PATCHING','VALIDATING')"
            ),
        ),
        Index("ix_incidents_list", text("created_at DESC"), text("id DESC")),
    )


class Occurrence(Base, Timestamped):
    __tablename__ = "occurrences"
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=new_id)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"))
    error_event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("error_events.id"), unique=True)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime())


class AnalysisRun(Base, Timestamped, Updated):
    __tablename__ = "analysis_runs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=new_id)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    base_commit_sha: Mapped[str] = mapped_column(String(64))
    input_error_event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("error_events.id"))
    policy_snapshot: Mapped[dict] = mapped_column(DOCUMENT, default=dict)
    codex_thread_id: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None] = mapped_column(DOCUMENT)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime())


class Approval(Base, Timestamped):
    __tablename__ = "approvals"
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=new_id)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"), unique=True)
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("analysis_runs.id"))
    idempotency_key: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), unique=True)
    base_commit_sha: Mapped[str] = mapped_column(String(64))


class PatchRun(Base, Timestamped, Updated):
    __tablename__ = "patch_runs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=new_id)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"), unique=True)
    approval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("approvals.id"), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    base_commit_sha: Mapped[str] = mapped_column(String(64))
    worktree_path: Mapped[str | None] = mapped_column(Text)
    changed_files: Mapped[list] = mapped_column(DOCUMENT, default=list)
    diff_text: Mapped[str | None] = mapped_column(Text)
    validation_result: Mapped[list] = mapped_column(DOCUMENT, default=list)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime())


class Job(Base, Timestamped, Updated):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=new_id)
    type: Mapped[str] = mapped_column(String(12))
    deduplication_key: Mapped[str] = mapped_column(String(200), unique=True)
    payload: Mapped[dict] = mapped_column(DOCUMENT)
    status: Mapped[str] = mapped_column(String(12), default="PENDING")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=now)
    locked_by: Mapped[str | None] = mapped_column(String(200))
    locked_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_detail: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint("type IN ('ANALYZE','PATCH')"),
        CheckConstraint("status IN ('PENDING','RUNNING','SUCCEEDED','FAILED')"),
        Index(
            "ix_jobs_claim",
            "status",
            "available_at",
            "created_at",
            "id",
            postgresql_where=text("status = 'PENDING'"),
            sqlite_where=text("status = 'PENDING'"),
        ),
        Index(
            "ix_jobs_stale",
            "locked_at",
            postgresql_where=text("status = 'RUNNING'"),
            sqlite_where=text("status = 'RUNNING'"),
        ),
    )


def make_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return create_engine(database_url, pool_pre_ping=True)
    if url.database and url.database != ":memory:":
        from pathlib import Path

        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        database_url,
        connect_args={"timeout": 30, "check_same_thread": False},
        poolclass=StaticPool if url.database == ":memory:" else None,
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    if url.database != ":memory:":
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    return engine


def make_session_factory(settings: Settings) -> sessionmaker:
    return sessionmaker(make_engine(settings.database_url), expire_on_commit=False)


@contextmanager
def write_transaction(factory: sessionmaker) -> Iterator[Session]:
    with factory.begin() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
        yield db
