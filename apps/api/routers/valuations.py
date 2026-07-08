"""Valuation reporting API router."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, selectinload

from apps.api.dependencies import CurrentUser, DateWindow, DbSession, PaginationParams, WriteAuthorizedUser
from apps.api.schemas.instruments import PaginationMeta
from apps.api.schemas.valuations import (
    AssumptionEntryResponse,
    AttributionDetailResponse,
    AttributionResponse,
    ConfidenceSnapshotResponse,
    ModelOutputResponse,
    RunValuationRequest,
    SensitivityResponse,
    SensitivityRowResponse,
    ValuationDetailResponse,
    ValuationListItem,
    ValuationListResponse,
    ValuationRunResponse,
    ValueBridgeEntry,
)
from avis.core.valuation_engine import (
    ConfidenceScoringEngine,
    DcfAssumptions,
    DcfEngine,
    DcfProjectionInput,
    RelativeEngine,
    RelativeInputBundle,
    RelativePeerPoint,
    ValuationOrchestrator,
)
from avis.db.models import (
    DqIncident,
    DqResult,
    FundMetricFact,
    FundStatementFact,
    RefCompany,
    RefExchange,
    RefInstrument,
    ValAssumptionSet,
    ValAttribution,
    ValConfidenceSnapshot,
    ValModelOutput,
    ValRun,
    ValSensitivity,
)

router = APIRouter(prefix="/valuations", tags=["valuations"])

REVENUE_FACT_CODE = "Revenue"
RUN_LABEL_KEY = "RUN_LABEL"
DA_SCHEDULE_KEY = "DCF_DA_SCHEDULE"
CAPEX_SCHEDULE_KEY = "DCF_CAPEX_SCHEDULE"
NWC_SCHEDULE_KEY = "DCF_NWC_DELTA_SCHEDULE"
METRIC_CODES = {
    "tax_rate": "TAX_RATE",
    "share_count": "SHARE_COUNT",
    "net_debt": "NET_DEBT",
    "enterprise_value": "ENTERPRISE_VALUE",
    "equity_value": "EQUITY_VALUE",
    "ebitda": "EBITDA",
    "earnings": "EARNINGS",
    "book_value": "BOOK_VALUE",
    "sales": "SALES",
    "depreciation_and_amortization": "DEPRECIATION_AND_AMORTIZATION",
    "capex": "CAPEX",
    "delta_nwc": "DELTA_NWC",
}


@router.post("/run", response_model=ValuationRunResponse)
async def run_valuation(
    payload: RunValuationRequest,
    db: DbSession,
    _current_user: WriteAuthorizedUser,
) -> ValuationRunResponse:
    response = await db.run_sync(lambda sync_session: _run_valuation_sync(sync_session, payload))
    await db.commit()
    return response


@router.get("/instrument/{instrument_uuid}", response_model=ValuationListResponse)
async def list_instrument_valuations(
    instrument_uuid: UUID,
    db: DbSession,
    _current_user: CurrentUser,
    pagination: PaginationParams,
    date_window: DateWindow,
    run_label: str | None = Query(default=None),
) -> ValuationListResponse:
    from_date, to_date = date_window
    instrument = await db.scalar(select(RefInstrument).where(RefInstrument.instrument_uuid == instrument_uuid))
    if instrument is None:
        raise _not_found("instrument_not_found", "Unknown instrument_uuid")

    stmt = (
        select(ValRun)
        .where(ValRun.instrument_id == instrument.instrument_id)
        .order_by(ValRun.as_of_ts.desc(), ValRun.val_run_id.desc())
    )
    count_stmt = select(func.count()).select_from(ValRun).where(ValRun.instrument_id == instrument.instrument_id)
    if from_date:
        start_dt = datetime.combine(from_date, datetime.min.time())
        stmt = stmt.where(ValRun.as_of_ts >= start_dt)
        count_stmt = count_stmt.where(ValRun.as_of_ts >= start_dt)
    if to_date:
        end_dt = datetime.combine(to_date, datetime.max.time())
        stmt = stmt.where(ValRun.as_of_ts <= end_dt)
        count_stmt = count_stmt.where(ValRun.as_of_ts <= end_dt)
    if run_label:
        stmt = stmt.join(
            ValAssumptionSet,
            (ValAssumptionSet.val_run_id == ValRun.val_run_id) & (ValAssumptionSet.assumption_key == RUN_LABEL_KEY),
        ).where(ValAssumptionSet.assumption_value_text == run_label)
        count_stmt = count_stmt.join(
            ValAssumptionSet,
            (ValAssumptionSet.val_run_id == ValRun.val_run_id) & (ValAssumptionSet.assumption_key == RUN_LABEL_KEY),
        ).where(ValAssumptionSet.assumption_value_text == run_label)

    total = int((await db.scalar(count_stmt)) or 0)
    runs = (await db.scalars(stmt.offset(pagination.offset).limit(pagination.page_size))).all()
    run_ids = [run.val_run_id for run in runs]
    outputs = await db.execute(select(ValModelOutput).where(ValModelOutput.val_run_id.in_(run_ids))) if run_ids else None
    confidence_rows = await db.execute(select(ValConfidenceSnapshot).where(ValConfidenceSnapshot.val_run_id.in_(run_ids))) if run_ids else None
    assumption_rows = await db.execute(
        select(ValAssumptionSet).where(ValAssumptionSet.val_run_id.in_(run_ids), ValAssumptionSet.assumption_key == RUN_LABEL_KEY)
    ) if run_ids else None

    outputs_by_run: dict[int, dict[str, ValModelOutput]] = {}
    for row in (outputs.scalars().all() if outputs is not None else []):
        outputs_by_run.setdefault(row.val_run_id, {})[row.model_name] = row
    confidence_by_run = {row.val_run_id: row for row in (confidence_rows.scalars().all() if confidence_rows is not None else [])}
    labels_by_run = {row.val_run_id: row.assumption_value_text for row in (assumption_rows.scalars().all() if assumption_rows is not None else [])}

    items: list[ValuationListItem] = []
    for run in runs:
        model_outputs = outputs_by_run.get(run.val_run_id, {})
        blended = model_outputs.get("BLENDED")
        dcf = model_outputs.get("DCF")
        relative = model_outputs.get("RELATIVE")
        confidence = confidence_by_run.get(run.val_run_id)
        items.append(
            ValuationListItem(
                val_run_id=run.val_run_id,
                run_uuid=run.run_uuid,
                as_of_ts=run.as_of_ts,
                status=run.status,
                run_label=labels_by_run.get(run.val_run_id),
                blended_value=blended.target_price if blended else None,
                dcf_value=dcf.target_price if dcf else None,
                relative_value=relative.target_price if relative else None,
                confidence_score=confidence.overall_confidence if confidence else None,
            )
        )
    return ValuationListResponse(
        items=items,
        pagination=PaginationMeta(page=pagination.page, page_size=pagination.page_size, total=total),
    )


@router.get("/{val_run_id}", response_model=ValuationDetailResponse)
async def get_valuation_run(
    val_run_id: int,
    db: DbSession,
    _current_user: CurrentUser,
) -> ValuationDetailResponse:
    stmt: Select[tuple[ValRun]] = (
        select(ValRun)
        .options(
            selectinload(ValRun.instrument),
            selectinload(ValRun.assumptions),
            selectinload(ValRun.model_outputs),
            selectinload(ValRun.attributions),
            selectinload(ValRun.confidence_snapshots),
        )
        .where(ValRun.val_run_id == val_run_id)
    )
    run = await db.scalar(stmt)
    if run is None:
        raise _not_found("valuation_not_found", "Unknown val_run_id")

    run_label = next((row.assumption_value_text for row in run.assumptions if row.assumption_key == RUN_LABEL_KEY), None)
    return ValuationDetailResponse(
        val_run_id=run.val_run_id,
        run_uuid=run.run_uuid,
        instrument_uuid=run.instrument.instrument_uuid,
        status=run.status,
        as_of_ts=run.as_of_ts,
        run_type=run.run_type,
        trigger_type=run.trigger_type,
        created_at=run.created_at,
        completed_at=run.completed_at,
        run_label=run_label,
        assumption_set=[_assumption_row_to_schema(row) for row in sorted(run.assumptions, key=lambda item: item.assumption_key)],
        model_outputs=[ModelOutputResponse.model_validate(row) for row in sorted(run.model_outputs, key=lambda item: item.model_name)],
        attributions=[AttributionResponse.model_validate(row) for row in sorted(run.attributions, key=lambda item: (item.driver_type, item.driver_key))],
        confidence_snapshots=[ConfidenceSnapshotResponse.model_validate(row) for row in sorted(run.confidence_snapshots, key=lambda item: item.created_at)],
    )


@router.get("/{val_run_id}/attribution", response_model=AttributionDetailResponse)
async def get_valuation_attribution(
    val_run_id: int,
    db: DbSession,
    _current_user: CurrentUser,
    prior_run_id: int | None = Query(default=None),
) -> AttributionDetailResponse:
    rows = (
        await db.scalars(
            select(ValAttribution)
            .where(ValAttribution.val_run_id == val_run_id)
            .order_by(ValAttribution.driver_type.asc(), ValAttribution.driver_key.asc())
        )
    ).all()
    if not rows:
        raise _not_found("valuation_not_found", "Unknown val_run_id")

    bridge: list[ValueBridgeEntry] = []
    if prior_run_id is not None:
        prior_rows = (
            await db.scalars(
                select(ValAttribution).where(ValAttribution.val_run_id == prior_run_id)
            )
        ).all()
        prior_map = {row.driver_key: row for row in prior_rows}
        for row in rows:
            if row.driver_key not in prior_map:
                continue
            prior = prior_map[row.driver_key]
            bridge.append(
                ValueBridgeEntry(
                    driver_key=row.driver_key,
                    current_value=row.impact_value_abs,
                    prior_value=prior.impact_value_abs,
                    delta_value=row.impact_value_abs - prior.impact_value_abs,
                )
            )

    return AttributionDetailResponse(
        val_run_id=val_run_id,
        rows=[AttributionResponse.model_validate(row) for row in rows],
        value_bridge=bridge,
    )


@router.get("/{val_run_id}/sensitivity", response_model=SensitivityResponse)
async def get_valuation_sensitivity(
    val_run_id: int,
    db: DbSession,
    _current_user: CurrentUser,
) -> SensitivityResponse:
    stmt = (
        select(ValSensitivity)
        .join(ValModelOutput, ValSensitivity.model_output_id == ValModelOutput.model_output_id)
        .where(ValModelOutput.val_run_id == val_run_id)
        .order_by(ValSensitivity.assumption_key.asc(), ValSensitivity.shock_label.asc())
    )
    rows = (await db.scalars(stmt)).all()
    if not rows:
        raise HTTPException(status_code=404, detail={"code": "sensitivity_not_found", "message": "sensitivity not yet computed"})
    return SensitivityResponse(val_run_id=val_run_id, rows=[SensitivityRowResponse.model_validate(row) for row in rows])


class DatabaseCompPeerMap:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_peer_ids(self, instrument_id: int, *, as_of_date: date) -> list[int]:
        target = self._session.execute(
            select(RefInstrument, RefCompany)
            .join(RefCompany, RefInstrument.company_id == RefCompany.company_id)
            .where(RefInstrument.instrument_id == instrument_id)
        ).first()
        if target is None:
            return []
        instrument, company = target
        if company.industry_code is None and company.sector_code is None:
            return []
        stmt = (
            select(RefInstrument.instrument_id)
            .join(RefCompany, RefInstrument.company_id == RefCompany.company_id)
            .where(
                RefInstrument.instrument_id != instrument_id,
                RefInstrument.is_active.is_(True),
                or_(RefInstrument.listing_date.is_(None), RefInstrument.listing_date <= as_of_date),
                or_(RefInstrument.delisting_date.is_(None), RefInstrument.delisting_date >= as_of_date),
            )
            .order_by(RefInstrument.instrument_id.asc())
        )
        if company.industry_code is not None:
            stmt = stmt.where(RefCompany.industry_code == company.industry_code)
        else:
            stmt = stmt.where(RefCompany.sector_code == company.sector_code)
        return list(self._session.scalars(stmt))


class DatabasePeerMetricsProvider:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_metrics(self, instrument_id: int, *, as_of_date: date) -> RelativePeerPoint:
        return _build_relative_peer_point(self._session, instrument_id, as_of_date=as_of_date)


def _run_valuation_sync(session: Session, payload: RunValuationRequest) -> ValuationRunResponse:
    instrument = session.scalar(select(RefInstrument).where(RefInstrument.instrument_uuid == payload.instrument_uuid))
    if instrument is None:
        raise _not_found("instrument_not_found", "Unknown instrument_uuid")

    as_of_ts = datetime.now(timezone.utc)
    dcf_assumptions = DcfAssumptions(
        wacc=payload.assumption_set.wacc,
        terminal_growth=payload.assumption_set.terminal_growth,
        forecast_years=payload.assumption_set.forecast_years,
        revenue_cagr=payload.assumption_set.revenue_cagr,
        ebit_margin=payload.assumption_set.ebit_margin,
    )
    projection_input = _build_dcf_projection_input(session, instrument.instrument_id, payload)
    relative_inputs = RelativeInputBundle(
        target_instrument_id=instrument.instrument_id,
        as_of_date=as_of_ts,
        target_metrics=_build_relative_peer_point(session, instrument.instrument_id, as_of_date=as_of_ts.date()),
    )
    orchestrator = ValuationOrchestrator(
        session=session,
        dcf_engine=DcfEngine(),
        relative_engine=RelativeEngine(
            comp_peer_map=DatabaseCompPeerMap(session),
            peer_metrics_provider=DatabasePeerMetricsProvider(session),
        ),
        confidence_engine=ConfidenceScoringEngine(),
    )
    result = orchestrator.run(
        instrument_id=instrument.instrument_id,
        as_of_ts=as_of_ts,
        dcf_assumptions=dcf_assumptions,
        dcf_projection_input=projection_input,
        relative_inputs=relative_inputs,
        data_quality_score=_data_quality_score(session),
        assumption_stability=Decimal("75.00"),
    )
    _store_assumption_metadata(session, result.val_run_id, payload)
    session.flush()

    relative_output = session.scalar(
        select(ValModelOutput).where(
            ValModelOutput.val_run_id == result.val_run_id,
            ValModelOutput.model_name == "RELATIVE",
        )
    )
    blended_output = session.scalar(
        select(ValModelOutput).where(
            ValModelOutput.val_run_id == result.val_run_id,
            ValModelOutput.model_name == "BLENDED",
        )
    )
    return ValuationRunResponse(
        val_run_id=result.val_run_id,
        blended_value=blended_output.target_price if blended_output is not None else result.blended_target_price,
        dcf_value=result.dcf_result.intrinsic_value_per_share,
        relative_value=relative_output.target_price if relative_output is not None else None,
        confidence_score=result.confidence_result.composite_score,
        override_required=result.analyst_override_required,
    )


def _build_dcf_projection_input(session: Session, instrument_id: int, payload: RunValuationRequest) -> DcfProjectionInput:
    forecast_years = payload.assumption_set.forecast_years
    revenue_base = _latest_revenue(session, instrument_id)
    tax_rate = _metric_value(session, instrument_id, METRIC_CODES["tax_rate"])
    share_count = _metric_value(session, instrument_id, METRIC_CODES["share_count"])
    net_debt = _metric_value(session, instrument_id, METRIC_CODES["net_debt"])
    da_schedule = _schedule_from_request_or_metric(
        payload.assumption_set.da,
        fallback=_metric_value(session, instrument_id, METRIC_CODES["depreciation_and_amortization"]),
        forecast_years=forecast_years,
    )
    capex_schedule = _schedule_from_request_or_metric(
        payload.assumption_set.capex,
        fallback=_metric_value(session, instrument_id, METRIC_CODES["capex"]),
        forecast_years=forecast_years,
    )
    nwc_schedule = _schedule_from_request_or_metric(
        payload.assumption_set.nwc_delta,
        fallback=_metric_value(session, instrument_id, METRIC_CODES["delta_nwc"]),
        forecast_years=forecast_years,
    )
    return DcfProjectionInput(
        revenue_base=revenue_base,
        tax_rate=tax_rate,
        share_count=share_count,
        net_debt=net_debt,
        depreciation_and_amortization=tuple(da_schedule),
        capex=tuple(capex_schedule),
        delta_nwc=tuple(nwc_schedule),
    )


def _schedule_from_request_or_metric(values: list[Decimal] | None, *, fallback: Decimal, forecast_years: int) -> list[Decimal]:
    if values is not None:
        if len(values) != forecast_years:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_schedule_length", "message": "Schedule lengths must match forecast_years"},
            )
        return list(values)
    return [fallback for _ in range(forecast_years)]


def _build_relative_peer_point(session: Session, instrument_id: int, *, as_of_date: date) -> RelativePeerPoint:
    return RelativePeerPoint(
        instrument_id=instrument_id,
        enterprise_value=_metric_value(session, instrument_id, METRIC_CODES["enterprise_value"], as_of_date=as_of_date),
        equity_value=_metric_value(session, instrument_id, METRIC_CODES["equity_value"], as_of_date=as_of_date),
        ebitda=_metric_value(session, instrument_id, METRIC_CODES["ebitda"], as_of_date=as_of_date),
        earnings=_metric_value(session, instrument_id, METRIC_CODES["earnings"], as_of_date=as_of_date),
        book_value=_metric_value(session, instrument_id, METRIC_CODES["book_value"], as_of_date=as_of_date),
        sales=_metric_value(session, instrument_id, METRIC_CODES["sales"], as_of_date=as_of_date),
        net_debt=_metric_value(session, instrument_id, METRIC_CODES["net_debt"], as_of_date=as_of_date),
        share_count=_metric_value(session, instrument_id, METRIC_CODES["share_count"], as_of_date=as_of_date),
    )


def _metric_value(session: Session, instrument_id: int, metric_code: str, *, as_of_date: date | None = None) -> Decimal:
    stmt = select(FundMetricFact.metric_value).where(
        FundMetricFact.instrument_id == instrument_id,
        FundMetricFact.metric_code == metric_code,
    )
    if as_of_date is not None:
        stmt = stmt.where(FundMetricFact.as_of_date <= as_of_date)
    stmt = stmt.order_by(FundMetricFact.as_of_date.desc(), FundMetricFact.metric_fact_id.desc()).limit(1)
    value = session.scalar(stmt)
    if value is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "missing_metric", "message": f"Missing required metric: {metric_code}"},
        )
    return Decimal(value)


def _latest_revenue(session: Session, instrument_id: int) -> Decimal:
    stmt = (
        select(FundStatementFact.value)
        .where(
            FundStatementFact.instrument_id == instrument_id,
            FundStatementFact.line_item_code == REVENUE_FACT_CODE,
        )
        .order_by(FundStatementFact.as_reported_ts.desc(), FundStatementFact.statement_fact_id.desc())
        .limit(1)
    )
    value = session.scalar(stmt)
    if value is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "missing_metric", "message": "Missing required metric: Revenue"},
        )
    return Decimal(value)


def _data_quality_score(session: Session) -> Decimal:
    open_incidents = session.scalar(
        select(func.count())
        .select_from(DqIncident)
        .where(DqIncident.incident_status.in_(("OPEN", "IN_PROGRESS")))
    ) or 0
    return Decimal("20.00") if int(open_incidents) > 0 else Decimal("85.00")


def _store_assumption_metadata(session: Session, val_run_id: int, payload: RunValuationRequest) -> None:
    metadata_rows: list[tuple[str, str, str]] = [(RUN_LABEL_KEY, payload.run_label, "TEXT")]
    if payload.assumption_set.da is not None:
        metadata_rows.append((DA_SCHEDULE_KEY, json.dumps([str(value) for value in payload.assumption_set.da]), "JSON"))
    if payload.assumption_set.capex is not None:
        metadata_rows.append((CAPEX_SCHEDULE_KEY, json.dumps([str(value) for value in payload.assumption_set.capex]), "JSON"))
    if payload.assumption_set.nwc_delta is not None:
        metadata_rows.append((NWC_SCHEDULE_KEY, json.dumps([str(value) for value in payload.assumption_set.nwc_delta]), "JSON"))

    for key, text_value, unit in metadata_rows:
        row = ValAssumptionSet(
            val_run_id=val_run_id,
            assumption_key=key,
            assumption_value_numeric=None,
            assumption_value_text=text_value,
            assumption_unit=unit,
            source_type="ANALYST",
            source_reference="api.run",
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        _assign_pk_if_sqlite(session, row, "assumption_set_id", ValAssumptionSet)
        session.add(row)


def _assign_pk_if_sqlite(session: Session, instance: Any, pk_name: str, model: type[Any]) -> None:
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "sqlite":
        return
    if getattr(instance, pk_name, None) is not None:
        return
    current_max = session.scalar(select(func.max(getattr(model, pk_name))))
    setattr(instance, pk_name, 1 if current_max is None else int(current_max) + 1)


def _assumption_row_to_schema(row: ValAssumptionSet) -> AssumptionEntryResponse:
    return AssumptionEntryResponse(
        key=row.assumption_key,
        numeric_value=row.assumption_value_numeric,
        text_value=row.assumption_value_text,
        unit=row.assumption_unit,
        source_type=row.source_type,
        source_reference=row.source_reference,
    )


def _not_found(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": code, "message": message})
