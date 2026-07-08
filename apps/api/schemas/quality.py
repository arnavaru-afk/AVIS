from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from apps.api.schemas import AvisSchema
from apps.api.schemas.instruments import PaginationMeta


class DqResultResponse(AvisSchema):
    dq_result_id: int
    rule_code: str
    rule_name: str
    domain: str
    severity: str
    status: str
    target_table: str
    target_record_key: str | None
    failure_reason: str | None
    measured_value: str | None
    threshold_value: str | None
    evaluated_at: datetime


class IncidentListItem(AvisSchema):
    incident_id: int
    incident_status: str
    impact_level: str
    opened_at: datetime
    assigned_to: str | None
    severity: str
    domain: str


class IncidentListResponse(AvisSchema):
    items: list[IncidentListItem]
    pagination: PaginationMeta


class IncidentDetailResponse(AvisSchema):
    incident_id: int
    incident_status: str
    impact_level: str
    assigned_to: str | None
    opened_at: datetime
    resolved_at: datetime | None
    resolution_notes: str | None
    dq_result: DqResultResponse


class IncidentOverrideRequest(AvisSchema):
    justification: str = Field(min_length=20)
    analyst_id: UUID


class IncidentOverrideResponse(AvisSchema):
    incident_id: int
    override_id: int
    incident_status: str
    resolution_notes: str
