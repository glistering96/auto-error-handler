from __future__ import annotations

import json
import uuid
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from aeh.config import Settings
from aeh.contracts import utc
from aeh.db import INCIDENT_STATES
from aeh.errors import AehError
from aeh.repository import ReviewRepository
from aeh.service import analysis_response, incident_detail, list_incidents, patch_response


class ReviewService:
    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db
        self.repository = ReviewRepository(db)

    def list_page(
        self, service_key: str | None, state: str | None, cursor: str | None, limit: int
    ) -> dict:
        if state and state not in INCIDENT_STATES:
            raise AehError(400, "AEH-EVENT-400-001", "Unknown incident state")
        page = list_incidents(self.settings, self.db, service_key, state, cursor, limit)
        next_query = None
        if page["nextCursor"]:
            next_query = urlencode(
                {
                    "serviceKey": service_key or "",
                    "state": state or "",
                    "cursor": page["nextCursor"],
                    "limit": limit,
                }
            )
        return {
            "items": page["items"],
            "next_query": next_query,
            "service_key": service_key or "",
            "state": state or "",
            "states": INCIDENT_STATES,
            "limit": limit,
        }

    def detail_page(self, incident_id: uuid.UUID) -> dict:
        incident = self.repository.incident(incident_id)
        if incident is None:
            raise AehError(404, "AEH-EVENT-404-001", "Incident not found")
        first_event = self.repository.first_event(incident)
        if first_event is None:
            raise AehError(500, "AEH-INTERNAL-500-001", "Incident input event is missing")
        analysis = self.repository.analysis(incident_id)
        approval = self.repository.approval(incident_id)
        patch = self.repository.patch(incident_id)
        occurrences = self.repository.recent_occurrences(incident_id)
        timeline = [{"label": "오류 접수", "time": utc(incident.created_at)}]
        if analysis and analysis.started_at:
            timeline.append({"label": "분석 시작", "time": utc(analysis.started_at)})
        if analysis and analysis.finished_at:
            timeline.append({"label": "분석 종료", "time": utc(analysis.finished_at)})
        if approval:
            timeline.append({"label": "패치 승인", "time": utc(approval.created_at)})
        if patch and patch.started_at:
            timeline.append({"label": "패치 시작", "time": utc(patch.started_at)})
        if patch and patch.finished_at:
            timeline.append({"label": "패치 종료", "time": utc(patch.finished_at)})
        return {
            "incident": incident_detail(self.db, incident),
            "first_event": first_event.normalized_payload,
            "first_event_json": json.dumps(
                first_event.normalized_payload, ensure_ascii=False, indent=2
            ),
            "analysis": analysis_response(self.db, analysis) if analysis else None,
            "approval": {"id": str(approval.id), "createdAt": utc(approval.created_at)}
            if approval
            else None,
            "patch": patch_response(patch) if patch else None,
            "occurrences": [
                {
                    "eventId": str(event.event_id),
                    "occurredAt": utc(occurrence.occurred_at),
                    "message": event.normalized_payload.get("message", ""),
                }
                for occurrence, event in occurrences
            ],
            "timeline": timeline,
        }
