from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import rfc8785
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, ConfigDict

from aeh.config import Limits
from aeh.errors import AehError, scrub

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas/external-error-event-v1.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
EVENT_VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
SENSITIVE_KEY = re.compile(
    r"(?i)(authorization|cookie|password|secret|token|api[_-]?key|credential|private[_-]?key)"
)


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AehError(400, "AEH-EVENT-400-001", "JSON object has a duplicate key")
        result[key] = value
    return result


def parse_event(raw: bytes, idempotency_key: str | None) -> tuple[dict, str, dict]:
    if len(raw) > Limits.body_bytes:
        raise AehError(413, "AEH-EVENT-413-001", "Request body exceeds 256 KiB")
    try:
        event = json.loads(
            raw,
            object_pairs_hook=_unique_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite number")),
        )
    except AehError:
        raise
    except (ValueError, UnicodeDecodeError) as exc:
        raise AehError(400, "AEH-EVENT-400-001", "Invalid JSON request body") from exc
    if not isinstance(event, dict):
        raise AehError(422, "AEH-EVENT-422-001", "Event must be a JSON object")
    errors = sorted(EVENT_VALIDATOR.iter_errors(event), key=lambda e: list(map(str, e.path)))
    if errors:
        raise AehError(422, "AEH-EVENT-422-001", errors[0].message)
    try:
        header_id = uuid.UUID(idempotency_key or "")
    except ValueError as exc:
        raise AehError(400, "AEH-EVENT-400-001", "Idempotency-Key must be a UUID") from exc
    if str(header_id) != str(uuid.UUID(event["eventId"])):
        raise AehError(400, "AEH-EVENT-400-001", "Idempotency-Key must match eventId")
    fingerprint = unicodedata.normalize("NFC", event["fingerprint"].strip())
    if not fingerprint or len(fingerprint) > 200:
        raise AehError(422, "AEH-EVENT-422-001", "fingerprint is empty or too long")
    check_reproduction_data(event.get("reproductionData"))
    try:
        checksum = hashlib.sha256(rfc8785.dumps(event)).hexdigest()
    except (ValueError, OverflowError, TypeError) as exc:
        raise AehError(
            422, "AEH-EVENT-422-001", "Event cannot be normalized as RFC 8785 JSON"
        ) from exc
    normalized = redact(event)
    normalized["fingerprint"] = fingerprint
    return event, checksum, normalized


def check_reproduction_data(value: Any) -> None:
    if value is None:
        return
    count = 0

    def visit(node: Any, depth: int) -> None:
        nonlocal count
        count += 1
        if depth > Limits.reproduction_depth or count > Limits.reproduction_values:
            raise AehError(
                422, "AEH-EVENT-422-001", "reproductionData exceeds depth or value limit"
            )
        if isinstance(node, dict):
            for key, child in node.items():
                if SENSITIVE_KEY.search(key):
                    raise AehError(
                        422, "AEH-EVENT-422-001", "reproductionData contains a sensitive field"
                    )
                visit(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                visit(child, depth + 1)
        elif isinstance(node, str) and (
            len(node) > Limits.reproduction_string or scrub(node) != node
        ):
            raise AehError(
                422,
                "AEH-EVENT-422-001",
                "reproductionData contains an oversized or sensitive value",
            )

    visit(value, 0)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("[REDACTED]" if SENSITIVE_KEY.search(key) else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return scrub(value, max(len(value), 1))
    return value


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    symbol: str | None
    explanation: str


class ProposedChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    purpose: str
    operation: Literal["modify", "add", "delete"]


class ReproductionAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reproducible: bool
    reason: str
    suggestedRegressionTests: list[str]


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str
    rootCause: str
    confidence: Literal["low", "medium", "high"]
    evidence: list[Evidence]
    proposedChanges: list[ProposedChange]
    validationPlan: list[str]
    reproductionAssessment: ReproductionAssessment
    risk: Literal["low", "medium", "high"]
    missingInformation: list[str]

    def useful(self, allowed_paths: list[str]) -> bool:
        from fnmatch import fnmatchcase

        return (
            self.confidence != "low"
            and bool(self.evidence)
            and bool(self.proposedChanges)
            and all(
                any(fnmatchcase(change.path, pat) for pat in allowed_paths)
                for change in self.proposedChanges
            )
        )


def utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
