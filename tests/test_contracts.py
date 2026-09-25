import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]


def test_event_schema_and_openapi_contract():
    schema = json.loads((ROOT / "schemas/external-error-event-v1.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    event = json.loads((ROOT / "fixtures/event.json").read_text())
    assert validator.is_valid(event)
    assert not validator.is_valid({**event, "eventId": "bad-uuid"})
    assert not validator.is_valid({**event, "occurredAt": "bad-date"})
    api = yaml.safe_load((ROOT / "openapi/auto-error-handler-mvp.v1.yaml").read_text())
    assert api["openapi"] == "3.1.0"
    assert api["paths"]["/v1/error-events"]["post"]["responses"]["202"]
    approve = api["paths"]["/v1/incidents/{incidentId}/approve"]["post"]
    assert "requestBody" not in approve
    assert any(item.get("name") == "Idempotency-Key" for item in approve["parameters"])
