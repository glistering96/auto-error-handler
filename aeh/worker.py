from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from aeh.config import Limits, Settings
from aeh.contracts import AnalysisResult
from aeh.db import (
    AnalysisRun,
    ErrorEvent,
    Incident,
    make_session_factory,
    now,
)
from aeh.errors import AehError
from aeh.gateway import CodexGateway, FakeCodexGateway, SdkCodexGateway
from aeh.gitops import assert_clean, check_diff, resolve_commit, worktree
from aeh.job_repository import (
    Claim,
    Outcome,
    claim_job,
    finalize,
    heartbeat,
    mark_validating,
    recover_expired,
)
from aeh.validation import ValidationFailure, run_validation

log = logging.getLogger("aeh.worker")


@asynccontextmanager
async def isolated_worktree(
    repo: Path, root: Path, kind: str, run_id: str, sha: str
) -> AsyncIterator[Path]:
    manager = worktree(repo, root, kind, run_id, sha)
    entering = asyncio.create_task(asyncio.to_thread(manager.__enter__))
    try:
        path = await asyncio.shield(entering)
    except asyncio.CancelledError:
        await entering
        await asyncio.to_thread(manager.__exit__, None, None, None)
        raise
    try:
        yield path
    finally:
        cleanup = asyncio.create_task(asyncio.to_thread(manager.__exit__, None, None, None))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise


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
        repo = settings.resolve_repository_reference(incident.repository_path_snapshot)
        sha = incident.base_commit_sha
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
    if await asyncio.to_thread(resolve_commit, repo, sha, "main") != sha:
        raise AehError(422, "AEH-GIT-422-001", "Stored commit changed")
    if claim.type == "ANALYZE":
        elapsed = (now() - analysis_started).total_seconds() if analysis_started else 0
        remaining = Limits.analysis_seconds - elapsed
        if remaining <= 0:
            raise AehError(504, "AEH-JOB-504-001", "Analysis lifetime exceeded 24 hours")
        async with isolated_worktree(
            repo, settings.workspace_root, "analysis", str(uuid.uuid4()), sha
        ) as workspace:
            async with asyncio.timeout(min(policy["analysisTimeoutSeconds"], remaining)):
                result, thread_id = await gateway.analyze(workspace, event, policy)
            await asyncio.to_thread(assert_clean, workspace)
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
    async with isolated_worktree(
        repo, settings.workspace_root, "patch", str(uuid.uuid4()), sha
    ) as workspace:
        if analysis is None:
            raise AehError(500, "AEH-INTERNAL-500-001", "Approved analysis result is missing")
        async with asyncio.timeout(min(policy["patchTimeoutSeconds"], Limits.patch_seconds)):
            thread_id = await gateway.patch(workspace, analysis, policy)
            changed, diff = await asyncio.to_thread(check_diff, workspace, policy)
            version_ref[0] = await asyncio.to_thread(mark_validating, factory, claim)
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
    return await asyncio.to_thread(finalize, factory, claim, outcome, version_ref[0])


async def worker_loop(
    settings: Settings, factory: sessionmaker, gateway: CodexGateway, stop: asyncio.Event
) -> None:
    while not stop.is_set():
        recovered = await asyncio.to_thread(recover_expired, factory)
        if recovered:
            log.info("jobs_recovered count=%s", recovered)
        claim = await asyncio.to_thread(claim_job, settings, factory)
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
