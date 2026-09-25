import asyncio
import json
import os
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from aeh.cli import init_fixture
from aeh.config import Limits, Settings
from aeh.contracts import parse_event
from aeh.db import (
    AnalysisRun,
    Approval,
    Base,
    ErrorEvent,
    Incident,
    Job,
    Occurrence,
    PatchRun,
    Service,
    make_session_factory,
)
from aeh.errors import AehError
from aeh.gateway import FakeCodexGateway, SdkCodexGateway
from aeh.service import sync_services
from aeh.validation import ValidationFailure, run_validation
from aeh.worker import Outcome, claim_job, finalize, heartbeat, recover_expired, run_one
from apps.control_api.main import create_app
from apps.review_web.main import create_app as create_review_app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["sqlite", "postgresql"] if os.getenv("TEST_POSTGRES_URL") else ["sqlite"])
def system(tmp_path, request):
    database_url = (
        os.environ["TEST_POSTGRES_URL"]
        if request.param == "postgresql"
        else f"sqlite+pysqlite:///{tmp_path / 'test.db'}"
    )
    settings = Settings(
        database_url=database_url,
        service_config_path=ROOT / "services.example.yaml",
        repository_root=tmp_path / "repositories",
        workspace_root=tmp_path / "workspaces",
        use_fake_codex=True,
    )
    init_fixture(settings)
    factory = make_session_factory(settings)
    if request.param == "postgresql":
        Base.metadata.drop_all(factory.kw["bind"])
    Base.metadata.create_all(factory.kw["bind"])
    sync_services(settings, factory)
    with TestClient(create_app(settings, factory)) as client:
        yield settings, factory, client


def event_data() -> dict:
    return json.loads((ROOT / "fixtures/event.json").read_text())


def send(client: TestClient, event: dict):
    return client.post(
        "/v1/error-events", json=event, headers={"Idempotency-Key": event["eventId"]}
    )


def test_event_contract_and_canonical_checksum():
    event = event_data()
    raw = json.dumps(event).encode()
    _, checksum, _ = parse_event(raw, event["eventId"])
    _, reordered, _ = parse_event(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode(), event["eventId"]
    )
    assert checksum == reordered
    with pytest.raises(AehError):
        parse_event(b'{"eventId":"a","eventId":"b"}', event["eventId"])
    event["eventId"] = "not-uuid"
    with pytest.raises(AehError):
        parse_event(json.dumps(event).encode(), event["eventId"])
    event = event_data()
    event["occurredAt"] = "not-a-date"
    with pytest.raises(AehError):
        parse_event(json.dumps(event).encode(), event["eventId"])
    event = event_data()
    event["reproductionData"]["request"]["body"] = {"Authorization": "Bearer example"}
    with pytest.raises(AehError):
        parse_event(json.dumps(event).encode(), event["eventId"])


def test_full_fake_flow_and_idempotency(system):
    settings, factory, client = system
    first = event_data()
    accepted = send(client, first)
    assert accepted.status_code == 202, accepted.text
    incident_id = accepted.json()["incidentId"]
    for _ in range(3):
        repeat = send(client, first)
        assert repeat.status_code == 202 and repeat.json()["duplicate"]
        assert repeat.json()["incidentId"] == incident_id
    additional = {**first, "eventId": str(uuid.uuid4())}
    assert send(client, additional).json()["incidentId"] == incident_id
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ErrorEvent)) == 2
        assert db.scalar(select(func.count()).select_from(Occurrence)) == 2
        assert db.scalar(select(func.count()).select_from(Job)) == 1
        assert db.get(Incident, uuid.UUID(incident_id)).version == 1
    claim = claim_job(settings, factory)
    assert claim and claim.type == "ANALYZE"
    assert asyncio.run(run_one(settings, factory, FakeCodexGateway(), claim))
    analysis = client.get(f"/v1/incidents/{incident_id}/analysis")
    assert analysis.json()["status"] == "SUCCEEDED"
    assert analysis.json()["inputEventId"] == first["eventId"]
    assert "repositoryPath" not in analysis.json()["policySummary"]
    assert client.get(f"/v1/incidents/{incident_id}/patch").status_code == 404
    assert (
        client.post(
            f"/v1/incidents/{incident_id}/approve",
            json={"x": 1},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        ).status_code
        == 400
    )
    key = str(uuid.uuid4())
    approval = client.post(f"/v1/incidents/{incident_id}/approve", headers={"Idempotency-Key": key})
    assert approval.status_code == 202, approval.text
    assert client.post(
        f"/v1/incidents/{incident_id}/approve", headers={"Idempotency-Key": key}
    ).json()["duplicate"]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Approval)) == 1
        assert db.scalar(select(func.count()).select_from(PatchRun)) == 1
    claim = claim_job(settings, factory)
    assert claim and claim.type == "PATCH"
    assert asyncio.run(run_one(settings, factory, FakeCodexGateway(), claim))
    patch = client.get(f"/v1/incidents/{incident_id}/patch")
    assert patch.status_code == 200
    assert patch.json()["status"] == "PATCH_READY", patch.text
    assert patch.json()["changedFiles"] == ["src/handler.py"]
    assert patch.json()["validation"][0]["status"] == "PASSED"
    assert 'return {"status": 404}' in patch.json()["diff"]
    assert client.get(f"/v1/incidents/{incident_id}").json()["state"] == "PATCH_READY"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 2
    assert not any((settings.workspace_root / "patch").iterdir())


def test_review_web_shows_receipt_analysis_and_patch(system):
    settings, factory, client = system
    event = event_data()
    event["message"] = "<script>alert(1)</script>"
    incident_id = send(client, event).json()["incidentId"]
    with TestClient(create_review_app(settings, factory)) as review:
        overview = review.get("/review/incidents")
        assert overview.status_code == 200
        assert incident_id in overview.text
        assert "오류 처리 내역" in overview.text
        first = review.get(f"/review/incidents/{incident_id}")
        assert first.status_code == 200
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in first.text
        assert "<script>alert(1)</script>" not in first.text
        claim = claim_job(settings, factory)
        assert asyncio.run(run_one(settings, factory, FakeCodexGateway(), claim))
        analyzed = review.get(f"/review/incidents/{incident_id}")
        assert "Fixture API returns an error" in analyzed.text
        client.post(
            f"/v1/incidents/{incident_id}/approve",
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        claim = claim_job(settings, factory)
        assert asyncio.run(run_one(settings, factory, FakeCodexGateway(), claim))
        patched = review.get(f"/review/incidents/{incident_id}")
        assert "PATCH_READY" in patched.text
        assert "Git diff" in patched.text
        assert "PASSED" in patched.text


def test_old_claim_cannot_heartbeat_or_finalize(system):
    settings, factory, client = system
    send(client, event_data())
    old = claim_job(settings, factory)
    assert heartbeat(factory, old)
    with factory.begin() as db:
        job = db.get(Job, old.job_id)
        job.locked_at = job.locked_at.replace(year=2000)
    assert recover_expired(factory) == 1
    assert not heartbeat(factory, old)
    assert not finalize(factory, old, Outcome(result={}))
    with factory.begin() as db:
        job = db.get(Job, old.job_id)
        job.available_at = func.now()
    new = claim_job(settings, factory)
    assert new and new.job_id == old.job_id and new.token != old.token
    assert not finalize(factory, old, Outcome(result={}))


def test_lost_lease_cancels_running_analysis(system, monkeypatch):
    settings, factory, client = system
    send(client, event_data())
    claim = claim_job(settings, factory)
    assert claim

    class SlowGateway(FakeCodexGateway):
        async def analyze(self, workspace, event, policy):
            await asyncio.sleep(60)
            return await super().analyze(workspace, event, policy)

    def lose_lease(*_):
        with factory.begin() as db:
            db.get(Job, claim.job_id).claim_token = uuid.uuid4()
        return False

    monkeypatch.setattr(Limits, "heartbeat_seconds", 0.01)
    monkeypatch.setattr("aeh.worker.heartbeat", lose_lease)
    assert not asyncio.run(run_one(settings, factory, SlowGateway(), claim))
    assert not any((settings.workspace_root / "analysis").iterdir())


def test_two_workers_do_not_claim_one_job(system):
    settings, factory, client = system
    send(client, event_data())
    workers = [
        settings.model_copy(update={"worker_id": "worker-a"}),
        settings.model_copy(update={"worker_id": "worker-b"}),
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda worker: claim_job(worker, factory), workers))
    assert sum(claim is not None for claim in claims) == 1
    with factory() as db:
        job = db.scalar(select(Job))
        assert job.locked_by in {"worker-a", "worker-b"}


def test_parallel_receipt_preserves_idempotency_and_grouping(system):
    _, factory, client = system
    event = event_data()
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: send(client, event), range(8)))
    assert all(response.status_code == 202 for response in responses)
    assert sum(not response.json()["duplicate"] for response in responses) == 1
    incident_id = responses[0].json()["incidentId"]
    events = [{**event, "eventId": str(uuid.uuid4())} for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        grouped = list(pool.map(lambda item: send(client, item), events))
    assert all(response.status_code == 202 for response in grouped)
    assert {response.json()["incidentId"] for response in grouped} == {incident_id}
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ErrorEvent)) == 9
        assert db.scalar(select(func.count()).select_from(Occurrence)) == 9
        assert db.scalar(select(func.count()).select_from(Job)) == 1
        assert db.get(Incident, uuid.UUID(incident_id)).occurrence_count == 9


def test_parallel_approval_key_cannot_approve_two_incidents(system):
    settings, factory, client = system
    first = event_data()
    second = {**first, "eventId": str(uuid.uuid4()), "fingerprint": "separate-incident"}
    incident_ids = [send(client, item).json()["incidentId"] for item in (first, second)]
    for _ in incident_ids:
        claim = claim_job(settings, factory)
        assert claim and asyncio.run(run_one(settings, factory, FakeCodexGateway(), claim))
    key = str(uuid.uuid4())

    def approve_one(incident_id):
        return client.post(f"/v1/incidents/{incident_id}/approve", headers={"Idempotency-Key": key})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(approve_one, incident_ids))
    assert sorted(result.status_code for result in results) == [202, 409]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Approval)) == 1
        assert db.scalar(select(func.count()).select_from(PatchRun)) == 1


def test_approval_respects_global_job_limit(system, monkeypatch):
    settings, factory, client = system
    first = event_data()
    incident_id = send(client, first).json()["incidentId"]
    claim = claim_job(settings, factory)
    assert claim and asyncio.run(run_one(settings, factory, FakeCodexGateway(), claim))
    second = {**first, "eventId": str(uuid.uuid4()), "fingerprint": "another-pending-job"}
    assert send(client, second).status_code == 202
    monkeypatch.setattr(Limits, "job_limit", 1)
    response = client.post(
        f"/v1/incidents/{incident_id}/approve",
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 503
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Approval)) == 0
        assert db.get(Incident, uuid.UUID(incident_id)).state == "AWAITING_APPROVAL"


def test_worker_uses_its_own_repository_root(system, tmp_path):
    settings, factory, client = system
    incident_id = send(client, event_data()).json()["incidentId"]
    replica_root = tmp_path / "replica-repositories"
    replica_root.mkdir()
    shutil.copytree(settings.repository_root / "fixture-api", replica_root / "fixture-api")
    replica = settings.model_copy(
        update={
            "repository_root": replica_root,
            "workspace_root": tmp_path / "replica-workspaces",
        }
    )
    claim = claim_job(replica, factory)
    assert claim and asyncio.run(run_one(replica, factory, FakeCodexGateway(), claim))
    assert client.get(f"/v1/incidents/{incident_id}/analysis").json()["status"] == "SUCCEEDED"


def test_policy_snapshot_survives_service_update(system):
    settings, factory, client = system
    accepted = send(client, event_data()).json()
    claim = claim_job(settings, factory)
    assert claim and asyncio.run(run_one(settings, factory, FakeCodexGateway(), claim))
    with factory.begin() as db:
        run = db.scalar(
            select(AnalysisRun).where(AnalysisRun.incident_id == uuid.UUID(accepted["incidentId"]))
        )
        original = run.policy_snapshot
        service = db.get(Incident, run.incident_id)
        row = db.get(Service, service.service_id)
        row.policy = {**row.policy, "maxDiffLines": 1}
    with factory() as db:
        run = db.scalar(
            select(AnalysisRun).where(AnalysisRun.incident_id == uuid.UUID(accepted["incidentId"]))
        )
        assert run.policy_snapshot == original
    approval = client.post(
        f"/v1/incidents/{accepted['incidentId']}/approve",
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert approval.status_code == 202
    patch = claim_job(settings, factory)
    assert patch and asyncio.run(run_one(settings, factory, FakeCodexGateway(), patch))
    assert (
        client.get(f"/v1/incidents/{accepted['incidentId']}/patch").json()["status"]
        == "PATCH_READY"
    )


def test_request_errors_and_sha_grouping(system):
    settings, factory, client = system
    event = event_data()
    assert (
        client.post(
            "/v1/error-events",
            content=b" " * (256 * 1024 + 1),
            headers={"Idempotency-Key": event["eventId"]},
        ).status_code
        == 413
    )
    first = send(client, event)
    assert first.status_code == 202
    changed = {**event, "message": "different request"}
    assert send(client, changed).status_code == 409
    bad_sha = {
        **event,
        "eventId": str(uuid.uuid4()),
        "release": {"version": "1", "commitSha": "abcdef0"},
    }
    assert send(client, bad_sha).status_code == 422
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ErrorEvent)) == 1
    # A new commit with the same fingerprint starts a separate incident.
    repo = settings.repository_root / "fixture-api"

    subprocess.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "-m", "new release"],
        check=True,
        capture_output=True,
    )
    second = {**event, "eventId": str(uuid.uuid4())}
    response = send(client, second)
    assert response.status_code == 202
    assert response.json()["incidentId"] != first.json()["incidentId"]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Incident)) == 2


def test_validation_network_is_blocked(system):
    settings, _, _ = system
    workspace = settings.repository_root / "fixture-api"
    policy = {
        "validationCommands": [
            {
                "id": "network",
                "argv": [
                    "python3",
                    "-c",
                    "import socket; s=socket.socket(); assert s.connect_ex(('1.1.1.1', 443)) != 0",
                ],
                "timeoutSeconds": 5,
            }
        ]
    }
    assert run_validation(workspace, policy)[0]["status"] == "PASSED"


def test_validation_output_is_bounded_and_timeout_is_reported(system):
    settings, _, _ = system
    workspace = settings.repository_root / "fixture-api"
    noisy = {
        "validationCommands": [
            {
                "id": "noisy",
                "argv": ["python3", "-c", "print('x' * 100000)"],
                "timeoutSeconds": 5,
            }
        ]
    }
    result = run_validation(workspace, noisy)[0]
    assert result["status"] == "PASSED"
    assert len(result["output"].encode()) <= Limits.stream_bytes
    sleepy = {
        "validationCommands": [
            {
                "id": "sleepy",
                "argv": ["python3", "-c", "import time; time.sleep(10)"],
                "timeoutSeconds": 1,
            }
        ]
    }
    with pytest.raises(ValidationFailure) as caught:
        run_validation(workspace, sleepy)
    assert caught.value.results[0]["status"] == "TIMED_OUT"


def test_policy_violation_stops_validation(system):
    settings, factory, client = system
    incident_id = send(client, event_data()).json()["incidentId"]
    analysis = claim_job(settings, factory)
    assert asyncio.run(run_one(settings, factory, FakeCodexGateway(), analysis))
    client.post(
        f"/v1/incidents/{incident_id}/approve", headers={"Idempotency-Key": str(uuid.uuid4())}
    )

    class UnsafeGateway(FakeCodexGateway):
        async def patch(self, workspace, analysis, policy):
            (workspace / "README.md").write_text("unsafe")
            return "unsafe"

    patch = claim_job(settings, factory)
    assert asyncio.run(run_one(settings, factory, UnsafeGateway(), patch))
    response = client.get(f"/v1/incidents/{incident_id}/patch").json()
    assert response["status"] == "PATCH_FAILED"
    assert response["validation"] == []


def test_registered_validation_failure_stops_patch(system):
    settings, factory, client = system
    incident_id = send(client, event_data()).json()["incidentId"]
    analysis = claim_job(settings, factory)
    assert asyncio.run(run_one(settings, factory, FakeCodexGateway(), analysis))
    client.post(
        f"/v1/incidents/{incident_id}/approve", headers={"Idempotency-Key": str(uuid.uuid4())}
    )

    class BrokenPatch(FakeCodexGateway):
        async def patch(self, workspace, analysis, policy):
            target = workspace / "src/handler.py"
            target.write_text(
                target.read_text().replace("return None  # BUG", 'return {"status": 500}')
            )
            return "broken-patch"

    claim = claim_job(settings, factory)
    assert asyncio.run(run_one(settings, factory, BrokenPatch(), claim))
    result = client.get(f"/v1/incidents/{incident_id}/patch").json()
    assert result["status"] == "PATCH_FAILED"
    assert result["validation"][0]["status"] == "FAILED"


def test_analysis_lifetime_is_terminal(system):
    settings, factory, client = system
    incident_id = send(client, event_data()).json()["incidentId"]
    claim = claim_job(settings, factory)
    with factory.begin() as db:
        run = db.scalar(
            select(AnalysisRun).where(AnalysisRun.incident_id == uuid.UUID(incident_id))
        )
        run.started_at = run.started_at.replace(year=2000)
    assert asyncio.run(run_one(settings, factory, FakeCodexGateway(), claim))
    with factory() as db:
        job = db.get(Job, claim.job_id)
        assert job.status == "FAILED" and job.attempt == 1
    assert client.get(f"/v1/incidents/{incident_id}/analysis").json()["status"] == "FAILED"


def test_api_key_mode_is_explicit_and_isolated(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    gateway = SdkCodexGateway(Settings(codex_auth_mode="api-key"))
    with pytest.raises(AehError), gateway._config():
        pass
    monkeypatch.setenv("OPENAI_API_KEY", "local-test-value")
    with gateway._config() as config:
        assert config.env["CODEX_API_KEY"] == "local-test-value"
        assert config.env["OPENAI_API_KEY"] == ""
        assert Path(config.env["CODEX_HOME"]).is_dir()
        assert not (Path(config.env["CODEX_HOME"]) / "auth.json").exists()
