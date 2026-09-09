"""Pipeline operations API router."""

from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import Select, select
from sqlalchemy.orm import selectinload

from apps.api.dependencies import CurrentUser, DbSession
from apps.api.schemas.pipeline import (
    JobEventResponse,
    PipelineRunDetailResponse,
    PipelineRunListItem,
    PipelineRunListResponse,
)
from avis.db.models import OpsPipelineRun

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.get("/runs", response_model=PipelineRunListResponse)
async def list_pipeline_runs(
    db: DbSession,
    _current_user: CurrentUser,
    status: str | None = Query(default=None),
    pipeline_name: str | None = Query(default=None),
) -> PipelineRunListResponse:
    stmt: Select[tuple[OpsPipelineRun]] = select(OpsPipelineRun).order_by(OpsPipelineRun.started_at.desc())
    if status:
        stmt = stmt.where(OpsPipelineRun.status == status.upper())
    if pipeline_name:
        stmt = stmt.where(OpsPipelineRun.pipeline_name == pipeline_name)
    rows = (await db.scalars(stmt)).all()
    return PipelineRunListResponse(items=[PipelineRunListItem.model_validate(row) for row in rows])


@router.get("/runs/{run_id}", response_model=PipelineRunDetailResponse)
async def get_pipeline_run(
    run_id: int,
    db: DbSession,
    _current_user: CurrentUser,
) -> PipelineRunDetailResponse:
    stmt: Select[tuple[OpsPipelineRun]] = (
        select(OpsPipelineRun)
        .options(selectinload(OpsPipelineRun.job_events), selectinload(OpsPipelineRun.sla_breaches))
        .where(OpsPipelineRun.pipeline_run_id == run_id)
    )
    run = await db.scalar(stmt)
    if run is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail={"code": "run_not_found", "message": "Unknown run_id"})

    return PipelineRunDetailResponse(
        pipeline_run_id=run.pipeline_run_id,
        run_uuid=run.run_uuid,
        pipeline_name=run.pipeline_name,
        run_mode=run.run_mode,
        triggered_by=run.triggered_by,
        status=run.status,
        started_at=run.started_at,
        ended_at=run.ended_at,
        run_context=run.run_context,
        sla_breach=bool(run.sla_breaches),
        job_events=[JobEventResponse.model_validate(event) for event in sorted(run.job_events, key=lambda item: item.event_ts)],
    )
