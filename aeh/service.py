from __future__ import annotations

import base64
import hmac
import uuid
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from aeh.config import Limits, Settings
from aeh.contracts import utc
from aeh.db import (
    AnalysisRun,
    Approval,
    ErrorEvent,
    Incident,
    Job,
    Occurrence,
    PatchRun,
    Service,
    write_transaction,
)
from aeh.errors import AehError
from aeh.gitops import resolve_commit
from aeh.repository import IncidentRepository, ReviewRepository


def sync_services(settings: Settings, factory: sessionmaker) -> None:
    policies = settings.load_services()
    with write_transaction(factory) as db:
        repository = IncidentRepository(db)
        for key, policy in policies.items():
            path = policy.repository_path
            settings.repository_path(policy)
            row = repository.service_by_key(key, lock=True)
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
                moved = settings.resolve_repository_reference(
                    row.repository_path
                ) != settings.resolve_repository_reference(path)
                if moved and repository.has_open_incident(row.id):
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
    repository = IncidentRepository(db)
    row = repository.event_by_identity(service_id, event_id)
    if row is None:
        return None
    if row.payload_checksum != checksum:
        raise AehError(409, "AEH-EVENT-409-001", "eventId was already used with another payload")
    incident = repository.incident_for_event(row.id)
    if incident is None:
        raise AehError(500, "AEH-INTERNAL-500-001", "Event has no incident")
    return {
        "eventId": str(event_id),
        "incidentId": str(incident.id),
        "status": incident.state,
        "duplicate": True,
    }


def _require_job_capacity(db: Session, repository: IncidentRepository) -> None:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(394771029)"))
    if repository.active_job_count() >= Limits.job_limit:
        raise AehError(503, "AEH-JOB-503-001", "Pending job limit reached")


def ingest(
    settings: Settings, factory: sessionmaker, event: dict, checksum: str, normalized: dict
) -> dict:
    event_id = uuid.UUID(event["eventId"])
    with factory() as db:
        service = IncidentRepository(db).service_by_key(event["serviceKey"], active=True)
        if service is None:
            raise AehError(404, "AEH-EVENT-404-001", "Unknown serviceKey")
        duplicate = _duplicate(db, service.id, event_id, checksum)
        if duplicate:
            return duplicate
        service_id, repo_path, branch = service.id, service.repository_path, service.default_branch
    sha = resolve_commit(
        settings.resolve_repository_reference(repo_path),
        event.get("release", {}).get("commitSha"),
        branch,
    )
    for _ in range(3):
        try:
            return _ingest_resolved(
                factory, service_id, repo_path, event, event_id, checksum, normalized, sha
            )
        except IntegrityError as exc:
            sqlstate = getattr(exc.orig, "sqlstate", None)
            if sqlstate != "23505" and "UNIQUE constraint failed" not in str(exc.orig):
                raise
    raise AehError(503, "AEH-JOB-503-001", "Concurrent event receipt did not settle; retry")


def _ingest_resolved(
    factory: sessionmaker,
    service_id: uuid.UUID,
    repo_path: str,
    event: dict,
    event_id: uuid.UUID,
    checksum: str,
    normalized: dict,
    sha: str,
) -> dict:
    with write_transaction(factory) as db:
        repository = IncidentRepository(db)
        service = repository.service_by_id(service_id, read_lock=True)
        if service is None or not service.active:
            raise AehError(404, "AEH-EVENT-404-001", "Unknown serviceKey")
        duplicate = _duplicate(db, service.id, event_id, checksum)
        if duplicate:
            return duplicate
        # Repository changes serialize with new incident creation; a changed path requires a new Git check.
        if service.repository_path != repo_path:
            raise AehError(503, "AEH-GIT-503-001", "Repository changed during event receipt; retry")
        incident = repository.open_incident(service.id, normalized["fingerprint"], sha)
        if incident is None:
            _require_job_capacity(db, repository)
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


def incident_summary(db: Session, incident: Incident, service_key: str | None = None) -> dict:
    if service_key is None:
        service = IncidentRepository(db).service_by_id(incident.service_id)
        if service is None:
            raise AehError(500, "AEH-INTERNAL-500-001", "Incident service is missing")
        service_key = service.key
    return {
        "id": str(incident.id),
        "serviceKey": service_key,
        "state": incident.state,
        "fingerprint": incident.fingerprint,
        "occurrenceCount": incident.occurrence_count,
        "createdAt": utc(incident.created_at),
        "updatedAt": utc(incident.updated_at),
    }


def incident_detail(db: Session, incident: Incident) -> dict:
    repository = ReviewRepository(db)
    event = repository.first_event(incident)
    if event is None:
        raise AehError(500, "AEH-INTERNAL-500-001", "Incident input event is missing")
    analysis = repository.analysis(incident.id)
    patch = repository.patch(incident.id)
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
    before = _cursor_decode(settings.cursor_secret, cursor) if cursor else None
    rows = IncidentRepository(db).list_rows(service_key, state, before, limit + 1)
    return {
        "items": [incident_summary(db, row, key) for row, key in rows[:limit]],
        "nextCursor": _cursor_encode(settings.cursor_secret, rows[limit - 1][0])
        if len(rows) > limit
        else None,
    }


def approve(factory: sessionmaker, incident_id: uuid.UUID, key: uuid.UUID) -> dict:
    try:
        return _approve_once(factory, incident_id, key)
    except IntegrityError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None)
        if sqlstate != "23505" and "UNIQUE constraint failed" not in str(exc.orig):
            raise
        with factory() as db:
            existing = db.scalar(select(Approval).where(Approval.idempotency_key == key))
            if existing and existing.incident_id != incident_id:
                raise AehError(
                    409, "AEH-APPROVAL-409-001", "Idempotency-Key belongs to another incident"
                ) from exc
        return _approve_once(factory, incident_id, key)


def _approve_once(factory: sessionmaker, incident_id: uuid.UUID, key: uuid.UUID) -> dict:
    with write_transaction(factory) as db:
        repository = IncidentRepository(db)
        incident = db.scalar(select(Incident).where(Incident.id == incident_id).with_for_update())
        if incident is None:
            raise AehError(404, "AEH-EVENT-404-001", "Incident not found")
        existing_key = repository.approval_by_key(key)
        if existing_key:
            if existing_key.incident_id != incident_id:
                raise AehError(
                    409, "AEH-APPROVAL-409-001", "Idempotency-Key belongs to another incident"
                )
            patch = repository.patch_by_approval(existing_key.id)
            if patch is None:
                raise AehError(500, "AEH-INTERNAL-500-001", "Approval has no patch run")
            return {
                "incidentId": str(incident.id),
                "approvalId": str(existing_key.id),
                "patchRunId": str(patch.id),
                "status": "PATCHING",
                "duplicate": True,
            }
        if incident.state != "AWAITING_APPROVAL":
            raise AehError(409, "AEH-APPROVAL-409-001", "Incident is not awaiting approval")
        runs = repository.successful_analysis(incident.id)
        if (
            len(runs) != 1
            or not runs[0].policy_snapshot
            or runs[0].base_commit_sha != incident.base_commit_sha
        ):
            raise AehError(409, "AEH-APPROVAL-409-002", "Successful analysis and SHA do not match")
        if repository.approval_for_incident(incident.id):
            raise AehError(409, "AEH-APPROVAL-409-001", "Incident is already approved")
        _require_job_capacity(db, repository)
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
