"""Data quality incident API router."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import Select, func, select
from sqlalchemy.orm import selectinload

from apps.api.dependencies import CurrentUser, DbSession, PaginationParams, WriteAuthorizedUser
from apps.api.schemas.instruments import PaginationMeta
from apps.api.schemas.quality import (
    DqResultResponse,
    IncidentDetailResponse,
    IncidentListItem,
    IncidentListResponse,
    IncidentOverrideRequest,
    IncidentOverrideResponse,
)
from avis.db.models import AuthUser, DqIncident, DqOverride, DqResult, DqRule

router = APIRouter(prefix="/quality", tags=["quality"])


@router.get("/incidents", response_model=IncidentListResponse)
async def list_incidents(
    db: DbSession,
    _current_user: CurrentUser,
    pagination: PaginationParams,
    severity: str | None = Query(default=None),
    domain: str | None = Query(default=None),
    from_date: date | None = Query(default=None),
) -> IncidentListResponse:
    stmt = (
        select(DqIncident, DqRule)
        .join(DqResult, DqIncident.dq_result_id == DqResult.dq_result_id)
        .join(DqRule, DqResult.dq_rule_id == DqRule.dq_rule_id)
        .where(DqIncident.incident_status == "OPEN")
        .order_by(DqIncident.opened_at.desc())
    )
    count_stmt = (
        select(func.count())
        .select_from(DqIncident)
        .join(DqResult, DqIncident.dq_result_id == DqResult.dq_result_id)
        .join(DqRule, DqResult.dq_rule_id == DqRule.dq_rule_id)
        .where(DqIncident.incident_status == "OPEN")
    )
    if severity:
        stmt = stmt.where(DqRule.severity == severity.upper())
        count_stmt = count_stmt.where(DqRule.severity == severity.upper())
    if domain:
        stmt = stmt.where(DqRule.domain_name == domain)
        count_stmt = count_stmt.where(DqRule.domain_name == domain)
    if from_date:
        opened_after = datetime.combine(from_date, datetime.min.time())
        stmt = stmt.where(DqIncident.opened_at >= opened_after)
        count_stmt = count_stmt.where(DqIncident.opened_at >= opened_after)

    total = int((await db.scalar(count_stmt)) or 0)
    rows = (await db.execute(stmt.offset(pagination.offset).limit(pagination.page_size))).all()
    items = [
        IncidentListItem(
            incident_id=incident.dq_incident_id,
            incident_status=incident.incident_status,
            impact_level=incident.impact_level,
            opened_at=incident.opened_at,
            assigned_to=incident.assigned_to,
            severity=rule.severity,
            domain=rule.domain_name,
        )
        for incident, rule in rows
    ]
    return IncidentListResponse(
        items=items,
        pagination=PaginationMeta(page=pagination.page, page_size=pagination.page_size, total=total),
    )


@router.get("/incidents/{incident_id}", response_model=IncidentDetailResponse)
async def get_incident(
    incident_id: int,
    db: DbSession,
    _current_user: CurrentUser,
) -> IncidentDetailResponse:
    stmt: Select[tuple[DqIncident]] = (
        select(DqIncident)
        .options(selectinload(DqIncident.dq_result).selectinload(DqResult.rule))
        .where(DqIncident.dq_incident_id == incident_id)
    )
    incident = await db.scalar(stmt)
    if incident is None:
        raise HTTPException(status_code=404, detail={"code": "incident_not_found", "message": "Unknown incident_id"})

    dq_result = incident.dq_result
    rule = dq_result.rule
    return IncidentDetailResponse(
        incident_id=incident.dq_incident_id,
        incident_status=incident.incident_status,
        impact_level=incident.impact_level,
        assigned_to=incident.assigned_to,
        opened_at=incident.opened_at,
        resolved_at=incident.resolved_at,
        resolution_notes=incident.resolution_notes,
        dq_result=DqResultResponse(
            dq_result_id=dq_result.dq_result_id,
            rule_code=rule.rule_code,
            rule_name=rule.rule_name,
            domain=rule.domain_name,
            severity=rule.severity,
            status=dq_result.status,
            target_table=dq_result.target_table,
            target_record_key=dq_result.target_record_key,
            failure_reason=dq_result.failure_reason,
            measured_value=dq_result.measured_value,
            threshold_value=dq_result.threshold_value,
            evaluated_at=dq_result.evaluated_at,
        ),
    )


@router.post("/incidents/{incident_id}/override", response_model=IncidentOverrideResponse)
async def override_incident(
    incident_id: int,
    payload: IncidentOverrideRequest,
    db: DbSession,
    _current_user: WriteAuthorizedUser,
) -> IncidentOverrideResponse:
    result = await db.run_sync(lambda sync_session: _override_incident(sync_session, incident_id, payload.analyst_id, payload.justification))
    await db.commit()
    return result


def _override_incident(sync_session, incident_id: int, analyst_uuid: UUID, justification: str) -> IncidentOverrideResponse:
    incident = sync_session.get(DqIncident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail={"code": "incident_not_found", "message": "Unknown incident_id"})
    analyst = sync_session.scalar(select(AuthUser).where(AuthUser.user_uuid == analyst_uuid, AuthUser.status == "ACTIVE"))
    if analyst is None:
        raise HTTPException(status_code=404, detail={"code": "analyst_not_found", "message": "Unknown analyst_id"})

    override = DqOverride(
        dq_incident_id=incident.dq_incident_id,
        approved_by_user_id=analyst.user_id,
        override_reason=justification,
        override_expiry_ts=None,
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )
    _assign_pk_if_sqlite(sync_session, override, "dq_override_id", DqOverride)
    sync_session.add(override)
    incident.incident_status = "RESOLVED"
    incident.resolved_at = datetime.now(UTC).replace(tzinfo=None)
    incident.resolution_notes = justification
    sync_session.flush()
    return IncidentOverrideResponse(
        incident_id=incident.dq_incident_id,
        override_id=override.dq_override_id,
        incident_status=incident.incident_status,
        resolution_notes=incident.resolution_notes or justification,
    )


def _assign_pk_if_sqlite(session, instance, pk_name: str, model) -> None:
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "sqlite":
        return
    if getattr(instance, pk_name, None) is not None:
        return
    current_max = session.scalar(select(func.max(getattr(model, pk_name))))
    setattr(instance, pk_name, 1 if current_max is None else int(current_max) + 1)
