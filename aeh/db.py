from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from aeh.config import Settings


def now() -> datetime:
    return datetime.now(UTC)


def new_id() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    pass


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Updated:
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Service(Base, Timestamped, Updated):
    __tablename__ = "services"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    key: Mapped[str] = mapped_column(String(100), unique=True)
    repository_path: Mapped[str] = mapped_column(Text)
    default_branch: Mapped[str] = mapped_column(String(255))
    policy: Mapped[dict] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class ErrorEvent(Base, Timestamped):
    __tablename__ = "error_events"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("services.id"))
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    payload_checksum: Mapped[str] = mapped_column(String(64))
    normalized_payload: Mapped[dict] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (Index("uq_error_event_identity", "service_id", "event_id", unique=True),)


OPEN_STATES = ("RECEIVED", "ANALYZING", "AWAITING_APPROVAL", "PATCHING", "VALIDATING")
INCIDENT_STATES = OPEN_STATES + ("ANALYSIS_FAILED", "PATCH_READY", "PATCH_FAILED")


class Incident(Base, Timestamped, Updated):
    __tablename__ = "incidents"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
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
        ),
        Index("ix_incidents_list", text("created_at DESC"), text("id DESC")),
    )


class Occurrence(Base, Timestamped):
    __tablename__ = "occurrences"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"))
    error_event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("error_events.id"), unique=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AnalysisRun(Base, Timestamped, Updated):
    __tablename__ = "analysis_runs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    base_commit_sha: Mapped[str] = mapped_column(String(64))
    input_error_event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("error_events.id"))
    policy_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    codex_thread_id: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Approval(Base, Timestamped):
    __tablename__ = "approvals"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"), unique=True)
    analysis_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("analysis_runs.id"))
    idempotency_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    base_commit_sha: Mapped[str] = mapped_column(String(64))


class PatchRun(Base, Timestamped, Updated):
    __tablename__ = "patch_runs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"), unique=True)
    approval_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("approvals.id"), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    base_commit_sha: Mapped[str] = mapped_column(String(64))
    worktree_path: Mapped[str | None] = mapped_column(Text)
    changed_files: Mapped[list] = mapped_column(JSONB, default=list)
    diff_text: Mapped[str | None] = mapped_column(Text)
    validation_result: Mapped[list] = mapped_column(JSONB, default=list)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base, Timestamped, Updated):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    type: Mapped[str] = mapped_column(String(12))
    deduplication_key: Mapped[str] = mapped_column(String(200), unique=True)
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(12), default="PENDING")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    locked_by: Mapped[str | None] = mapped_column(String(200))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
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
        ),
        Index("ix_jobs_stale", "locked_at", postgresql_where=text("status = 'RUNNING'")),
    )


def make_session_factory(settings: Settings) -> sessionmaker:
    return sessionmaker(
        create_engine(settings.database_url, pool_pre_ping=True), expire_on_commit=False
    )
