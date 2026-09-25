from __future__ import annotations

import base64
import hmac
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from aeh.config import Limits, Settings
from aeh.contracts import utc
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
from aeh.errors import AehError
from aeh.gitops import resolve_commit


def sync_services(settings: Settings, factory: sessionmaker) -> None:
    policies = settings.load_services()
    with factory.begin() as db:
        for key, policy in policies.items():
            path = str(settings.repository_path(policy))
            row = db.scalar(select(Service).where(Service.key == key).with_for_update())
            if row is None:
                db.add(
                    Service(
                        key=key,
                        repository_path=path,
                        default_branch=policy.default_branch,
                        policy=policy.model_dump(by_alias=True),
                    )
                )
            else:
                if row.repository_path != path and db.scalar(
                    select(Incident.id)
                    .where(Incident.service_id == row.id, Incident.state.in_(OPEN_STATES))
                    .limit(1)
                ):
                    raise ValueError(
                        f"Service {key} has active incidents; repository path cannot change"
                    )
                row.repository_path = path
                row.default_branch = policy.default_branch
                row.policy = policy.model_dump(by_alias=True)
                row.active = True


def _duplicate(
    db: Session, service_id: uuid.UUID, event_id: uuid.UUID, checksum: str
) -> dict | None:
    row = db.scalar(
        select(ErrorEvent).where(
            ErrorEvent.service_id == service_id, ErrorEvent.event_id == event_id
        )
    )
    if row is None:
        return None
    if row.payload_checksum != checksum:
        raise AehError(409, "AEH-EVENT-409-001", "eventId was already used with another payload")
    incident = db.scalar(
        select(Incident)
        .join(Occurrence, Occurrence.incident_id == Incident.id)
        .where(Occurrence.error_event_id == row.id)
    )
    if incident is None:
        raise AehError(500, "AEH-INTERNAL-500-001", "Event has no incident")
    return {
        "eventId": str(event_id),
        "incidentId": str(incident.id),
        "status": incident.state,
        "duplicate": True,
    }


def ingest(
    settings: Settings, factory: sessionmaker, event: dict, checksum: str, normalized: dict
) -> dict:
    event_id = uuid.UUID(event["eventId"])
    with factory() as db:
        service = db.scalar(
            select(Service).where(Service.key == event["serviceKey"], Service.active.is_(True))
        )
        if service is None:
            raise AehError(404, "AEH-EVENT-404-001", "Unknown serviceKey")
        duplicate = _duplicate(db, service.id, event_id, checksum)
        if duplicate:
            return duplicate
        service_id, repo_path, branch = service.id, service.repository_path, service.default_branch
    sha = resolve_commit(Path(repo_path), event.get("release", {}).get("commitSha"), branch)
    with factory.begin() as db:
        service = db.scalar(select(Service).where(Service.id == service_id).with_for_update())
        if service is None or not service.active:
            raise AehError(404, "AEH-EVENT-404-001", "Unknown serviceKey")
        duplicate = _duplicate(db, service.id, event_id, checksum)
        if duplicate:
            return duplicate
        # Repository changes serialize with new incident creation; a changed path requires a new Git check.
        if service.repository_path != repo_path:
            raise AehError(503, "AEH-GIT-503-001", "Repository changed during event receipt; retry")
        incident = db.scalar(
            select(Incident)
            .where(
                Incident.service_id == service.id,
                Incident.fingerprint == normalized["fingerprint"],
                Incident.base_commit_sha == sha,
                Incident.state.in_(OPEN_STATES),
            )
            .with_for_update()
        )
        if incident is None:
            db.execute(text("SELECT pg_advisory_xact_lock(394771029)"))
            pending = db.scalar(
                select(func.count()).select_from(Job).where(Job.status.in_(["PENDING", "RUNNING"]))
            )
            if pending >= Limits.job_limit:
                raise AehError(503, "AEH-JOB-503-001", "Pending job limit reached")
        row = ErrorEvent(
            service_id=service.id,
            event_id=event_id,
            payload_checksum=checksum,
            normalized_payload=normalized,
        )
        db.add(row)
        db.flush()
        if incident is None:
            incident = Incident(
                service_id=service.id,
                fingerprint=normalized["fingerprint"],
                repository_path_snapshot=repo_path,
                base_commit_sha=sha,
                first_error_event_id=row.id,
                occurrence_count=1,
            )
            db.add(incident)
            db.flush()
            db.add(
                Job(
                    type="ANALYZE",
                    deduplication_key=f"ANALYZE:{incident.id}",
                    payload={"version": 1, "incidentId": str(incident.id)},
                    max_attempts=Limits.max_attempts,
                )
            )
        else:
            incident.occurrence_count += 1
        db.add(
            Occurrence(
                incident_id=incident.id,
                error_event_id=row.id,
                occurred_at=datetime.fromisoformat(event["occurredAt"]),
            )
        )
        return {
            "eventId": str(event_id),
            "incidentId": str(incident.id),
            "status": incident.state,
            "duplicate": False,
        }


def policy_summary(snapshot: dict) -> dict:
    return {
        "allowedPaths": snapshot.get("allowedPaths", []),
        "deniedPaths": snapshot.get("deniedPaths", []),
        "maxChangedFiles": snapshot.get("maxChangedFiles", 10),
        "maxDiffLines": snapshot.get("maxDiffLines", 400),
        "validationCommands": [
            {"id": c["id"], "timeoutSeconds": c["timeoutSeconds"]}
            for c in snapshot.get("validationCommands", [])
        ],
        "model": snapshot.get("model", ""),
    }


def analysis_response(db: Session, run: AnalysisRun) -> dict:
    input_event = db.get(ErrorEvent, run.input_error_event_id)
    if input_event is None:
        raise AehError(500, "AEH-INTERNAL-500-001", "Analysis input event is missing")
    return {
        "id": str(run.id),
        "incidentId": str(run.incident_id),
        "status": run.status,
        "baseCommitSha": run.base_commit_sha,
        "inputEventId": str(input_event.event_id),
        "policySummary": policy_summary(run.policy_snapshot),
        "codexThreadId": run.codex_thread_id,
        "result": run.result,
        "error": run.error_detail,
        "startedAt": utc(run.started_at) if run.started_at else None,
        "finishedAt": utc(run.finished_at) if run.finished_at else None,
    }


def patch_response(run: PatchRun) -> dict:
    return {
        "id": str(run.id),
        "incidentId": str(run.incident_id),
        "status": run.status,
        "baseCommitSha": run.base_commit_sha,
        "changedFiles": run.changed_files,
        "diff": run.diff_text,
        "validation": run.validation_result,
        "error": run.error_detail,
    }


def incident_summary(db: Session, incident: Incident) -> dict:
    service = db.get(Service, incident.service_id)
    if service is None:
        raise AehError(500, "AEH-INTERNAL-500-001", "Incident service is missing")
    return {
        "id": str(incident.id),
        "serviceKey": service.key,
        "state": incident.state,
        "fingerprint": incident.fingerprint,
        "occurrenceCount": incident.occurrence_count,
        "createdAt": utc(incident.created_at),
        "updatedAt": utc(incident.updated_at),
    }


def incident_detail(db: Session, incident: Incident) -> dict:
    event = db.get(ErrorEvent, incident.first_error_event_id)
    if event is None:
        raise AehError(500, "AEH-INTERNAL-500-001", "Incident input event is missing")
    analysis = db.scalar(select(AnalysisRun).where(AnalysisRun.incident_id == incident.id))
    patch = db.scalar(select(PatchRun).where(PatchRun.incident_id == incident.id))
    return {
        **incident_summary(db, incident),
        "version": incident.version,
        "environment": event.normalized_payload["environment"],
        "baseCommitSha": incident.base_commit_sha,
        "latestAnalysis": analysis_response(db, analysis) if analysis else None,
        "latestPatch": {"id": str(patch.id), "status": patch.status} if patch else None,
    }


def _cursor_encode(secret: str, row: Incident) -> str:
    data = f"{row.created_at.isoformat()}|{row.id}".encode()
    signature = hmac.digest(secret.encode(), data, "sha256")[:16]
    return base64.urlsafe_b64encode(data + b"|" + signature.hex().encode()).decode().rstrip("=")


def _cursor_decode(secret: str, cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data, signature = raw.rsplit(b"|", 1)
        if not hmac.compare_digest(
            hmac.digest(secret.encode(), data, "sha256")[:16].hex().encode(), signature
        ):
            raise ValueError("bad signature")
        timestamp, row_id = data.decode().split("|", 1)
        return datetime.fromisoformat(timestamp), uuid.UUID(row_id)
    except (ValueError, UnicodeError) as exc:
        raise AehError(400, "AEH-EVENT-400-001", "Invalid cursor") from exc


def list_incidents(
    settings: Settings,
    db: Session,
    service_key: str | None,
    state: str | None,
    cursor: str | None,
    limit: int,
) -> dict:
    if not 1 <= limit <= 100:
        raise AehError(400, "AEH-EVENT-400-001", "limit must be between 1 and 100")
    query = select(Incident).join(Service, Incident.service_id == Service.id)
    if service_key:
        query = query.where(Service.key == service_key)
    if state:
        query = query.where(Incident.state == state)
    if cursor:
        created, row_id = _cursor_decode(settings.cursor_secret, cursor)
        query = query.where(
            or_(
                Incident.created_at < created,
                and_(Incident.created_at == created, Incident.id < row_id),
            )
        )
    rows = db.scalars(
        query.order_by(Incident.created_at.desc(), Incident.id.desc()).limit(limit + 1)
    ).all()
    return {
        "items": [incident_summary(db, row) for row in rows[:limit]],
        "nextCursor": _cursor_encode(settings.cursor_secret, rows[limit - 1])
        if len(rows) > limit
        else None,
    }


def approve(factory: sessionmaker, incident_id: uuid.UUID, key: uuid.UUID) -> dict:
    with factory.begin() as db:
        incident = db.scalar(select(Incident).where(Incident.id == incident_id).with_for_update())
        if incident is None:
            raise AehError(404, "AEH-EVENT-404-001", "Incident not found")
        existing_key = db.scalar(select(Approval).where(Approval.idempotency_key == key))
        if existing_key:
            if existing_key.incident_id != incident_id:
                raise AehError(
                    409, "AEH-APPROVAL-409-001", "Idempotency-Key belongs to another incident"
                )
            patch = db.scalar(select(PatchRun).where(PatchRun.approval_id == existing_key.id))
            return {
                "incidentId": str(incident.id),
                "approvalId": str(existing_key.id),
                "patchRunId": str(patch.id),
                "status": "PATCHING",
                "duplicate": True,
            }
        if incident.state != "AWAITING_APPROVAL":
            raise AehError(409, "AEH-APPROVAL-409-001", "Incident is not awaiting approval")
        runs = db.scalars(
            select(AnalysisRun).where(
                AnalysisRun.incident_id == incident.id, AnalysisRun.status == "SUCCEEDED"
            )
        ).all()
        if (
            len(runs) != 1
            or not runs[0].policy_snapshot
            or runs[0].base_commit_sha != incident.base_commit_sha
        ):
            raise AehError(409, "AEH-APPROVAL-409-002", "Successful analysis and SHA do not match")
        if db.scalar(select(Approval.id).where(Approval.incident_id == incident.id)):
            raise AehError(409, "AEH-APPROVAL-409-001", "Incident is already approved")
        approval = Approval(
            incident_id=incident.id,
            analysis_run_id=runs[0].id,
            idempotency_key=key,
            base_commit_sha=incident.base_commit_sha,
        )
        db.add(approval)
        db.flush()
        patch = PatchRun(
            incident_id=incident.id,
            approval_id=approval.id,
            base_commit_sha=approval.base_commit_sha,
            changed_files=[],
            validation_result=[],
        )
        db.add(patch)
        db.flush()
        db.add(
            Job(
                type="PATCH",
                deduplication_key=f"PATCH:{approval.id}",
                payload={"version": 1, "incidentId": str(incident.id), "patchRunId": str(patch.id)},
                max_attempts=Limits.max_attempts,
            )
        )
        incident.state = "PATCHING"
        incident.version += 1
        return {
            "incidentId": str(incident.id),
            "approvalId": str(approval.id),
            "patchRunId": str(patch.id),
            "status": "PATCHING",
            "duplicate": False,
        }
