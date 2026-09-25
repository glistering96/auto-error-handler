from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from aeh.config import Limits, ServicePolicy, Settings
from aeh.contracts import AnalysisResult
from aeh.db import (
    AnalysisRun,
    ErrorEvent,
    Incident,
    Job,
    PatchRun,
    Service,
    make_session_factory,
    now,
)
from aeh.errors import AehError
from aeh.gateway import CodexGateway, FakeCodexGateway, SdkCodexGateway
from aeh.gitops import assert_clean, check_diff, resolve_commit, worktree
from aeh.validation import ValidationFailure, run_validation

log = logging.getLogger("aeh.worker")


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
    with factory.begin() as db:
        job = db.scalar(
            select(Job)
            .where(Job.status == "PENDING", Job.available_at <= func.now())
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
    with factory.begin() as db:
        updated = db.execute(
            update(Job)
            .where(
                Job.id == claim.job_id,
                Job.claim_token == claim.token,
                Job.status == "RUNNING",
                Job.locked_at > func.now() - timedelta(seconds=Limits.lease_seconds),
            )
            .values(locked_at=func.now())
        )
        return updated.rowcount == 1


def recover_expired(factory: sessionmaker) -> int:
    recovered = 0
    with factory.begin() as db:
        jobs = db.scalars(
            select(Job)
            .where(
                Job.status == "RUNNING",
                Job.locked_at <= func.now() - timedelta(seconds=Limits.lease_seconds),
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
                    run.status, run.error_code, run.error_detail, run.finished_at = (
                        "FAILED",
                        job.last_error_code,
                        job.last_error_detail,
                        now(),
                    )
                    incident.state = "ANALYSIS_FAILED"
                else:
                    patch = db.scalar(select(PatchRun).where(PatchRun.incident_id == incident.id))
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
    with factory.begin() as db:
        job = db.scalar(select(Job).where(Job.id == claim.job_id).with_for_update())
        incident = db.scalar(
            select(Incident).where(Incident.id == claim.incident_id).with_for_update()
        )
        if (
            job.claim_token != claim.token
            or job.status != "RUNNING"
            or incident.version != claim.version
        ):
            raise AehError(409, "AEH-POLICY-422-001", "Patch claim is no longer current")
        incident.state = "VALIDATING"
        incident.version += 1
        patch = db.scalar(select(PatchRun).where(PatchRun.incident_id == incident.id))
        patch.status = "VALIDATING"
        return incident.version


def finalize(
    factory: sessionmaker, claim: Claim, outcome: Outcome, version: int | None = None
) -> bool:
    with factory.begin() as db:
        job = db.scalar(select(Job).where(Job.id == claim.job_id).with_for_update())
        if (
            job is None
            or job.status != "RUNNING"
            or job.claim_token != claim.token
            or job.locked_at <= now() - timedelta(seconds=Limits.lease_seconds)
        ):
            return False
        incident = db.scalar(
            select(Incident).where(Incident.id == claim.incident_id).with_for_update()
        )
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


async def execute(
    settings: Settings,
    factory: sessionmaker,
    gateway: CodexGateway,
    claim: Claim,
    version_ref: list[int],
) -> Outcome:
    with factory() as db:
        incident = db.get(Incident, claim.incident_id)
        if incident is None:
            raise AehError(500, "AEH-INTERNAL-500-001", "Claim incident is missing")
        repo, sha = Path(incident.repository_path_snapshot), incident.base_commit_sha
        run = db.scalar(select(AnalysisRun).where(AnalysisRun.incident_id == incident.id))
        if run is None:
            raise AehError(500, "AEH-INTERNAL-500-001", "Analysis run is missing")
        policy = run.policy_snapshot
        input_event = db.get(ErrorEvent, run.input_error_event_id)
        if input_event is None:
            raise AehError(500, "AEH-INTERNAL-500-001", "Analysis input event is missing")
        event = input_event.normalized_payload
        analysis = AnalysisResult.model_validate(run.result) if run.result else None
        analysis_started = run.started_at
    if resolve_commit(repo, sha, "main") != sha:
        raise AehError(422, "AEH-GIT-422-001", "Stored commit changed")
    if claim.type == "ANALYZE":
        elapsed = (now() - analysis_started).total_seconds() if analysis_started else 0
        remaining = Limits.analysis_seconds - elapsed
        if remaining <= 0:
            raise AehError(504, "AEH-JOB-504-001", "Analysis lifetime exceeded 24 hours")
        with worktree(
            repo, settings.workspace_root, "analysis", str(uuid.uuid4()), sha
        ) as workspace:
            async with asyncio.timeout(min(policy["analysisTimeoutSeconds"], remaining)):
                result, thread_id = await gateway.analyze(workspace, event, policy)
            assert_clean(workspace)
            if not result.useful(policy["allowedPaths"]):
                raise AehError(
                    502,
                    "AEH-CODEX-502-002",
                    "Analysis has insufficient evidence or unsafe proposed paths",
                )
            data = result.model_dump()
            if len(json.dumps(data, ensure_ascii=False).encode()) > Limits.analysis_bytes:
                raise AehError(422, "AEH-POLICY-422-001", "Analysis result exceeds storage limit")
            return Outcome(result=data, thread_id=thread_id)
    with worktree(repo, settings.workspace_root, "patch", str(uuid.uuid4()), sha) as workspace:
        if analysis is None:
            raise AehError(500, "AEH-INTERNAL-500-001", "Approved analysis result is missing")
        async with asyncio.timeout(min(policy["patchTimeoutSeconds"], Limits.patch_seconds)):
            thread_id = await gateway.patch(workspace, analysis, policy)
            changed, diff = check_diff(workspace, policy)
            version_ref[0] = mark_validating(factory, claim)
            try:
                validation = await asyncio.to_thread(run_validation, workspace, policy)
            except ValidationFailure as exc:
                return Outcome(
                    thread_id=thread_id,
                    changed_files=changed,
                    diff=diff,
                    validation=exc.results,
                    error_code=exc.code,
                    error_detail=exc.detail,
                )
            return Outcome(
                thread_id=thread_id, changed_files=changed, diff=diff, validation=validation
            )


async def run_one(
    settings: Settings, factory: sessionmaker, gateway: CodexGateway, claim: Claim
) -> bool:
    version_ref = [claim.version]
    task = asyncio.create_task(execute(settings, factory, gateway, claim, version_ref))

    async def keep_lease() -> None:
        deadline = time.monotonic() + Limits.lease_seconds
        delay = Limits.heartbeat_seconds
        while not task.done():
            await asyncio.sleep(delay)
            if task.done():
                break
            try:
                owned = await asyncio.to_thread(heartbeat, factory, claim)
            except SQLAlchemyError:
                log.warning("job_heartbeat_failed jobId=%s", claim.job_id)
                if time.monotonic() < deadline:
                    delay = 2
                    continue
                owned = False
            if not owned:
                task.cancel()
                break
            deadline = time.monotonic() + Limits.lease_seconds
            delay = Limits.heartbeat_seconds

    keep = asyncio.create_task(keep_lease())
    try:
        outcome = await task
    except TimeoutError:
        code = "AEH-JOB-504-001" if claim.type == "ANALYZE" else "AEH-POLICY-422-001"
        outcome = Outcome(error_code=code, error_detail="Execution time limit exceeded")
    except AehError as exc:
        outcome = Outcome(
            error_code=exc.code,
            error_detail=exc.detail,
            transient=exc.code in {"AEH-CODEX-502-001", "AEH-GIT-503-001"},
        )
    except asyncio.CancelledError:
        outcome = Outcome(
            error_code="AEH-CODEX-502-001", error_detail="Worker lost its lease", transient=True
        )
    except Exception:
        log.exception("job_execution_failed", extra={"jobId": str(claim.job_id)})
        outcome = Outcome(
            error_code="AEH-INTERNAL-500-001", error_detail="Internal execution error"
        )
    finally:
        keep.cancel()
        try:
            await keep
        except asyncio.CancelledError:
            pass
    return finalize(factory, claim, outcome, version_ref[0])


async def worker_loop(
    settings: Settings, factory: sessionmaker, gateway: CodexGateway, stop: asyncio.Event
) -> None:
    while not stop.is_set():
        recovered = recover_expired(factory)
        if recovered:
            log.info("jobs_recovered count=%s", recovered)
        claim = claim_job(settings, factory)
        if claim:
            log.info(
                "job_claimed jobId=%s incidentId=%s attempt=%s",
                claim.job_id,
                claim.incident_id,
                claim.attempt,
            )
            run = asyncio.create_task(run_one(settings, factory, gateway, claim))
            stopping = asyncio.create_task(stop.wait())
            done, _ = await asyncio.wait({run, stopping}, return_when=asyncio.FIRST_COMPLETED)
            if stopping in done and not run.done():
                run.cancel()
            saved = await run
            stopping.cancel()
            log.info("job_finished jobId=%s saved=%s", claim.job_id, saved)
        else:
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_seconds)
            except TimeoutError:
                pass


async def main() -> None:
    settings = Settings()
    factory = make_session_factory(settings)
    gateway: CodexGateway = (
        FakeCodexGateway() if settings.use_fake_codex else SdkCodexGateway(settings)
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    await asyncio.gather(
        *(worker_loop(settings, factory, gateway, stop) for _ in range(settings.worker_concurrency))
    )
