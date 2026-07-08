from __future__ import annotations

from datetime import datetime
from uuid import UUID

from apps.api.schemas import AvisSchema


class PipelineRunListItem(AvisSchema):
    pipeline_run_id: int
    run_uuid: UUID
    pipeline_name: str
    run_mode: str
    triggered_by: str
    status: str
    started_at: datetime
    ended_at: datetime | None


class PipelineRunListResponse(AvisSchema):
    items: list[PipelineRunListItem]


class JobEventResponse(AvisSchema):
    job_event_id: int
    job_name: str
    event_type: str
    event_ts: datetime
    message: str | None
    metrics_payload: dict[str, object] | None


class PipelineRunDetailResponse(PipelineRunListItem):
    run_context: dict[str, object] | None
    sla_breach: bool
    job_events: list[JobEventResponse]
