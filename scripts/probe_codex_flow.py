"""Manual real-Codex end-to-end test against the local fixture and database."""

import asyncio
import json
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from aeh.cli import init_fixture
from aeh.config import Settings
from aeh.db import make_session_factory
from aeh.gateway import SdkCodexGateway
from aeh.service import sync_services
from aeh.worker import claim_job, run_one
from apps.control_api.main import create_app


def main() -> None:
    settings = Settings(use_fake_codex=False)
    init_fixture(settings)
    factory = make_session_factory(settings)
    sync_services(settings, factory)
    event = json.loads((Path(__file__).resolve().parents[1] / "fixtures/event.json").read_text())
    event["eventId"] = str(uuid.uuid4())
    event["fingerprint"] += ":" + event["eventId"]
    with TestClient(create_app(settings, factory)) as client:
        response = client.post(
            "/v1/error-events", json=event, headers={"Idempotency-Key": event["eventId"]}
        )
        response.raise_for_status()
        incident_id = response.json()["incidentId"]
        gateway = SdkCodexGateway(settings)
        claim = claim_job(settings, factory)
        assert claim and claim.type == "ANALYZE"
        assert asyncio.run(run_one(settings, factory, gateway, claim))
        analysis = client.get(f"/v1/incidents/{incident_id}/analysis").json()
        print("analysis:", analysis["status"], analysis.get("error"))
        assert analysis["status"] == "SUCCEEDED"
        key = str(uuid.uuid4())
        approval = client.post(
            f"/v1/incidents/{incident_id}/approve", headers={"Idempotency-Key": key}
        )
        approval.raise_for_status()
        claim = claim_job(settings, factory)
        assert claim and claim.type == "PATCH"
        assert asyncio.run(run_one(settings, factory, gateway, claim))
        patch = client.get(f"/v1/incidents/{incident_id}/patch").json()
        print("patch:", patch["status"], patch["changedFiles"], patch.get("error"))
        print("validation:", [(step["id"], step["status"]) for step in patch["validation"]])
        assert patch["status"] == "PATCH_READY"


if __name__ == "__main__":
    main()
