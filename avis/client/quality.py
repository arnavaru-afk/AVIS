"""Quality client wrapper for AVIS API."""

from __future__ import annotations

from avis.client.base import AVISClient
from avis.client.models import (
    IncidentDetail,
    IncidentListResponse,
    IncidentResolution,
    IncidentSummary,
)


class QualityClient(AVISClient):
    def list_incidents(self, severity: str | None = None) -> list[IncidentSummary] | None:
        if not self.enabled:
            return None
        page = 1
        items: list[IncidentSummary] = []
        total = None
        while total is None or len(items) < total:
            params = {"page": page, "page_size": 200}
            if severity:
                params["severity"] = severity
            payload = self._get("/api/v1/quality/incidents", params=params)
            parsed = self._parse_dataclass(payload, IncidentListResponse)
            items.extend(parsed.items)
            total = parsed.pagination.total
            if (page * parsed.pagination.page_size) >= parsed.pagination.total:
                break
            page += 1
        return items

    def get_incident(self, incident_id: int) -> IncidentDetail | None:
        payload = self._get(f"/api/v1/quality/incidents/{incident_id}")
        if payload is None:
            return None
        return self._parse_dataclass(payload, IncidentDetail)

    def resolve_incident(self, incident_id: int, justification: str, analyst_id: str) -> IncidentResolution | None:
        payload = self._post(
            f"/api/v1/quality/incidents/{incident_id}/override",
            json_body={"justification": justification, "analyst_id": analyst_id},
        )
        if payload is None:
            return None
        return self._parse_dataclass(payload, IncidentResolution)
