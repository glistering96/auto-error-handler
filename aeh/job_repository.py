from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from aeh.config import Limits, ServicePolicy, Settings
from aeh.db import AnalysisRun, Incident, Job, PatchRun, Service, now, write_transaction
from aeh.errors import AehError


@dataclass(frozen=True)
class Claim:
    job_id: uuid.UUID
    token: uuid.UUID
    incident_id: uuid.UUID
    type: str
    version: int
    attempt: int


@dataclass
class Outcome:
    result: dict | None = None
    thread_id: str | None = None
    changed_files: list[str] = field(default_factory=list)
    diff: str | None = None
    validation: list[dict] = field(default_factory=list)
    error_code: str | None = None
    error_detail: str | None = None
    transient: bool = False


def claim_job(settings: Settings, factory: sessionmaker) -> Claim | None:
    with write_transaction(factory) as db:
        job = db.scalar(
            select(Job)
            .where(Job.status == "PENDING", Job.available_at <= now())
            .order_by(Job.available_at, Job.created_at, Job.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        incident_id = uuid.UUID(job.payload["incidentId"])
        incident = db.scalar(select(Incident).where(Incident.id == incident_id).with_for_update())
        if incident is None:
            raise RuntimeError("Job refers to a missing incident")
        token = uuid.uuid4()
        job.status, job.claim_token, job.locked_by, job.locked_at = (
            "RUNNING",
            token,
            settings.worker_id,
            now(),
        )
        job.attempt += 1
        if job.type == "ANALYZE":
            run = db.scalar(select(AnalysisRun).where(AnalysisRun.incident_id == incident.id))
            if run is None:
                service = db.get(Service, incident.service_id)
                if service is None:
                    raise RuntimeError("Analysis job service is missing")
                policy = ServicePolicy.model_validate(service.policy)
                snapshot = settings.snapshot(policy, incident.repository_path_snapshot)
                run = AnalysisRun(
                    incident_id=incident.id,
                    base_commit_sha=incident.base_commit_sha,
                    input_error_event_id=incident.first_error_event_id,
                    policy_snapshot=snapshot,
                    status="RUNNING",
                    started_at=now(),
                )
                db.add(run)
            if incident.state == "RECEIVED":
                incident.state = "ANALYZING"
                incident.version += 1
        elif job.type == "PATCH":
            patch = db.scalar(select(PatchRun).where(PatchRun.incident_id == incident.id))
            if patch is None or incident.state not in ("PATCHING", "VALIDATING"):
                raise RuntimeError("Patch job has no approved patch run")
            patch.status = "RUNNING"
            if patch.started_at is None:
                patch.started_at = now()
            if incident.state == "VALIDATING":
                incident.state = "PATCHING"
                incident.version += 1
        return Claim(job.id, token, incident.id, job.type, incident.version, job.attempt)


def heartbeat(factory: sessionmaker, claim: Claim) -> bool:
    with write_transaction(factory) as db:
        updated = db.scalar(
            update(Job)
            .where(
                Job.id == claim.job_id,
                Job.claim_token == claim.token,
                Job.status == "RUNNING",
                Job.locked_at > now() - timedelta(seconds=Limits.lease_seconds),
            )
            .values(locked_at=now())
            .returning(Job.id)
        )
        return updated is not None


def recover_expired(factory: sessionmaker) -> int:
    recovered = 0
    with write_transaction(factory) as db:
        jobs = db.scalars(
            select(Job)
            .where(
                Job.status == "RUNNING",
                Job.locked_at <= now() - timedelta(seconds=Limits.lease_seconds),
            )
            .with_for_update(skip_locked=True)
            .limit(100)
        ).all()
        for job in jobs:
            incident = db.scalar(
                select(Incident)
                .where(Incident.id == uuid.UUID(job.payload["incidentId"]))
                .with_for_update()
            )
            if incident is None:
                raise RuntimeError("Expired job incident is missing")
            job.claim_token = None
            job.locked_by = None
            job.locked_at = None
            job.last_error_code = "AEH-CODEX-502-001"
            job.last_error_detail = "Worker lease expired"
            if job.attempt < job.max_attempts:
                job.status = "PENDING"
                job.available_at = now() + timedelta(seconds=(10 if job.attempt == 1 else 60))
            else:
                job.status = "FAILED"
                if job.type == "ANALYZE":
                    run = db.scalar(
                        select(AnalysisRun).where(AnalysisRun.incident_id == incident.id)
                    )
                    if run is None:
                        raise RuntimeError("Expired analysis run is missing")
                    run.status, run.error_code, run.error_detail, run.finished_at = (
                        "FAILED",
                        job.last_error_code,
                        job.last_error_detail,
                        now(),
                    )
                    incident.state = "ANALYSIS_FAILED"
                else:
                    patch = db.scalar(select(PatchRun).where(PatchRun.incident_id == incident.id))
                    if patch is None:
                        raise RuntimeError("Expired patch run is missing")
                    patch.status, patch.error_code, patch.error_detail, patch.finished_at = (
                        "PATCH_FAILED",
                        job.last_error_code,
                        job.last_error_detail,
                        now(),
                    )
                    incident.state = "PATCH_FAILED"
                incident.version += 1
            recovered += 1
    return recovered


def mark_validating(factory: sessionmaker, claim: Claim) -> int:
    with write_transaction(factory) as db:
        job = db.scalar(select(Job).where(Job.id == claim.job_id).with_for_update())
        incident = db.scalar(
            select(Incident).where(Incident.id == claim.incident_id).with_for_update()
        )
        if job is None or incident is None:
            raise AehError(409, "AEH-POLICY-422-001", "Patch claim is missing")
        if (
            job.claim_token != claim.token
            or job.status != "RUNNING"
            or incident.version != claim.version
        ):
            raise AehError(409, "AEH-POLICY-422-001", "Patch claim is no longer current")
        incident.state = "VALIDATING"
        incident.version += 1
        patch = db.scalar(select(PatchRun).where(PatchRun.incident_id == incident.id))
        if patch is None:
            raise AehError(409, "AEH-POLICY-422-001", "Patch run is missing")
        patch.status = "VALIDATING"
        return incident.version


def finalize(
    factory: sessionmaker, claim: Claim, outcome: Outcome, version: int | None = None
) -> bool:
    with write_transaction(factory) as db:
        job = db.scalar(select(Job).where(Job.id == claim.job_id).with_for_update())
        if (
            job is None
            or job.status != "RUNNING"
            or job.claim_token != claim.token
            or job.locked_at is None
            or job.locked_at <= now() - timedelta(seconds=Limits.lease_seconds)
        ):
            return False
        incident = db.scalar(
            select(Incident).where(Incident.id == claim.incident_id).with_for_update()
        )
        if incident is None:
            return False
        if incident.version != (version if version is not None else claim.version):
            return False
        if outcome.error_code and outcome.transient and job.attempt < job.max_attempts:
            job.status = "PENDING"
            job.available_at = now() + timedelta(seconds=10 if job.attempt == 1 else 60)
            job.last_error_code, job.last_error_detail = outcome.error_code, outcome.error_detail
        else:
            job.status = "FAILED" if outcome.error_code else "SUCCEEDED"
            job.last_error_code, job.last_error_detail = outcome.error_code, outcome.error_detail
            if claim.type == "ANALYZE":
                run = db.scalar(select(AnalysisRun).where(AnalysisRun.incident_id == incident.id))
                if run is None:
                    raise RuntimeError("Analysis run is missing during finalization")
                run.status = "FAILED" if outcome.error_code else "SUCCEEDED"
                run.result, run.codex_thread_id = outcome.result, outcome.thread_id
                run.error_code, run.error_detail, run.finished_at = (
                    outcome.error_code,
                    outcome.error_detail,
                    now(),
                )
                incident.state = "ANALYSIS_FAILED" if outcome.error_code else "AWAITING_APPROVAL"
            else:
                patch = db.scalar(select(PatchRun).where(PatchRun.incident_id == incident.id))
                if patch is None:
                    raise RuntimeError("Patch run is missing during finalization")
                patch.status = "PATCH_FAILED" if outcome.error_code else "PATCH_READY"
                patch.changed_files, patch.diff_text, patch.validation_result = (
                    outcome.changed_files,
                    outcome.diff,
                    outcome.validation,
                )
                patch.error_code, patch.error_detail, patch.finished_at = (
                    outcome.error_code,
                    outcome.error_detail,
                    now(),
                )
                incident.state = patch.status
            incident.version += 1
        job.claim_token = None
        job.locked_by = None
        job.locked_at = None
        return True
