from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from aeh.db import (
    OPEN_STATES,
    AnalysisRun,
    Approval,
    ErrorEvent,
    Incident,
    Job,
    Occurrence,
    PatchRun,
    Service,
)


class IncidentRepository:
    """Queries shared by incident intake, review and approval."""

    def __init__(self, db: Session):
        self.db = db

    def service_by_key(
        self, key: str, *, active: bool = False, lock: bool = False
    ) -> Service | None:
        query = select(Service).where(Service.key == key)
        if active:
            query = query.where(Service.active.is_(True))
        if lock:
            query = query.with_for_update()
        return self.db.scalar(query)

    def service_by_id(self, service_id: uuid.UUID, *, read_lock: bool = False) -> Service | None:
        query = select(Service).where(Service.id == service_id)
        if read_lock:
            query = query.with_for_update(read=True)
        return self.db.scalar(query)

    def has_open_incident(self, service_id: uuid.UUID) -> bool:
        return (
            self.db.scalar(
                select(Incident.id)
                .where(Incident.service_id == service_id, Incident.state.in_(OPEN_STATES))
                .limit(1)
            )
            is not None
        )

    def event_by_identity(self, service_id: uuid.UUID, event_id: uuid.UUID) -> ErrorEvent | None:
        return self.db.scalar(
            select(ErrorEvent).where(
                ErrorEvent.service_id == service_id, ErrorEvent.event_id == event_id
            )
        )

    def incident_for_event(self, event_row_id: uuid.UUID) -> Incident | None:
        return self.db.scalar(
            select(Incident)
            .join(Occurrence, Occurrence.incident_id == Incident.id)
            .where(Occurrence.error_event_id == event_row_id)
        )

    def open_incident(self, service_id: uuid.UUID, fingerprint: str, sha: str) -> Incident | None:
        return self.db.scalar(
            select(Incident)
            .where(
                Incident.service_id == service_id,
                Incident.fingerprint == fingerprint,
                Incident.base_commit_sha == sha,
                Incident.state.in_(OPEN_STATES),
            )
            .with_for_update()
        )

    def active_job_count(self) -> int:
        return (
            self.db.scalar(
                select(func.count()).select_from(Job).where(Job.status.in_(["PENDING", "RUNNING"]))
            )
            or 0
        )

    def list_rows(
        self,
        service_key: str | None,
        state: str | None,
        before: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[tuple[Incident, str]]:
        query = select(Incident, Service.key).join(Service, Incident.service_id == Service.id)
        if service_key:
            query = query.where(Service.key == service_key)
        if state:
            query = query.where(Incident.state == state)
        if before:
            created, row_id = before
            query = query.where(
                or_(
                    Incident.created_at < created,
                    and_(Incident.created_at == created, Incident.id < row_id),
                )
            )
        rows = self.db.execute(
            query.order_by(Incident.created_at.desc(), Incident.id.desc()).limit(limit)
        ).all()
        return [(incident, key) for incident, key in rows]

    def approval_by_key(self, key: uuid.UUID) -> Approval | None:
        return self.db.scalar(select(Approval).where(Approval.idempotency_key == key))

    def approval_for_incident(self, incident_id: uuid.UUID) -> Approval | None:
        return self.db.scalar(select(Approval).where(Approval.incident_id == incident_id))

    def successful_analysis(self, incident_id: uuid.UUID) -> list[AnalysisRun]:
        return list(
            self.db.scalars(
                select(AnalysisRun).where(
                    AnalysisRun.incident_id == incident_id, AnalysisRun.status == "SUCCEEDED"
                )
            ).all()
        )

    def patch_by_approval(self, approval_id: uuid.UUID) -> PatchRun | None:
        return self.db.scalar(select(PatchRun).where(PatchRun.approval_id == approval_id))


class ReviewRepository:
    """Read models used by the review service."""

    def __init__(self, db: Session):
        self.db = db

    def incident(self, incident_id: uuid.UUID) -> Incident | None:
        return self.db.get(Incident, incident_id)

    def first_event(self, incident: Incident) -> ErrorEvent | None:
        return self.db.get(ErrorEvent, incident.first_error_event_id)

    def analysis(self, incident_id: uuid.UUID) -> AnalysisRun | None:
        return self.db.scalar(select(AnalysisRun).where(AnalysisRun.incident_id == incident_id))

    def approval(self, incident_id: uuid.UUID) -> Approval | None:
        return self.db.scalar(select(Approval).where(Approval.incident_id == incident_id))

    def patch(self, incident_id: uuid.UUID) -> PatchRun | None:
        return self.db.scalar(select(PatchRun).where(PatchRun.incident_id == incident_id))

    def recent_occurrences(
        self, incident_id: uuid.UUID, limit: int = 20
    ) -> list[tuple[Occurrence, ErrorEvent]]:
        rows = self.db.execute(
            select(Occurrence, ErrorEvent)
            .join(ErrorEvent, ErrorEvent.id == Occurrence.error_event_id)
            .where(Occurrence.incident_id == incident_id)
            .order_by(Occurrence.occurred_at.desc(), Occurrence.id.desc())
            .limit(limit)
        ).all()
        return [(occurrence, event) for occurrence, event in rows]
