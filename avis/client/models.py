"""Dataclasses mirroring AVIS API response schemas."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class PaginationMeta:
    page: int
    page_size: int
    total: int


@dataclass(frozen=True, slots=True)
class SymbolAlias:
    alias_id: int
    source_system: str
    alias_symbol: str
    valid_from: date
    valid_to: date | None


@dataclass(frozen=True, slots=True)
class InstrumentResult:
    instrument_uuid: UUID | None
    symbol: str
    isin: str | None
    exchange: str | None
    company_name: str


@dataclass(frozen=True, slots=True)
class InstrumentListResponse:
    items: list[InstrumentResult]
    pagination: PaginationMeta


@dataclass(frozen=True, slots=True)
class InstrumentDetail:
    instrument_uuid: UUID
    symbol: str
    isin: str | None
    exchange: str
    company_name: str
    is_active: bool
    sector: str | None
    aliases: list[SymbolAlias]


@dataclass(frozen=True, slots=True)
class PriceHistoryRow:
    trade_date: date
    open_px: Decimal
    high_px: Decimal
    low_px: Decimal
    close_px: Decimal
    adjusted_close_px: Decimal | None
    volume: Decimal
    source_system: str


@dataclass(frozen=True, slots=True)
class PriceHistory:
    instrument_uuid: UUID
    adjusted: bool
    rows: list[PriceHistoryRow]


@dataclass(frozen=True, slots=True)
class ValuationJob:
    val_run_id: int
    run_uuid: UUID
    status: str
    queued_at: datetime
    blended_value: Decimal | None
    dcf_value: Decimal | None
    relative_value: Decimal | None
    confidence_score: Decimal | None
    override_required: bool | None


@dataclass(frozen=True, slots=True)
class AssumptionEntry:
    key: str
    numeric_value: Decimal | None
    text_value: str | None
    unit: str | None
    source_type: str
    source_reference: str | None


@dataclass(frozen=True, slots=True)
class ModelOutput:
    model_output_id: int
    model_name: str
    model_version: str
    equity_value: Decimal | None
    enterprise_value: Decimal | None
    target_price: Decimal | None
    weight: Decimal | None
    currency_code: str
    output_payload: dict[str, object] | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AttributionRow:
    attribution_id: int
    driver_type: str
    driver_key: str
    impact_value_abs: Decimal
    impact_value_pct: Decimal | None
    direction: str
    evidence_ref: dict[str, object] | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ConfidenceSnapshot:
    confidence_id: int
    overall_confidence: Decimal
    data_quality_score: Decimal
    management_credibility_score: Decimal
    industry_stability_score: Decimal
    forecast_accuracy_score: Decimal
    news_reliability_score: Decimal
    model_agreement_score: Decimal
    scenario_dispersion_score: Decimal
    scoring_version: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ValuationDetail:
    val_run_id: int
    run_uuid: UUID
    instrument_uuid: UUID
    status: str
    as_of_ts: datetime
    run_type: str
    trigger_type: str
    created_at: datetime
    completed_at: datetime | None
    run_label: str | None
    assumption_set: list[AssumptionEntry]
    model_outputs: list[ModelOutput]
    attributions: list[AttributionRow]
    confidence_snapshots: list[ConfidenceSnapshot]


@dataclass(frozen=True, slots=True)
class ValuationSummary:
    val_run_id: int
    run_uuid: UUID
    as_of_ts: datetime
    status: str
    run_label: str | None
    blended_value: Decimal | None
    dcf_value: Decimal | None
    relative_value: Decimal | None
    confidence_score: Decimal | None


@dataclass(frozen=True, slots=True)
class ValuationListResponse:
    items: list[ValuationSummary]
    pagination: PaginationMeta


@dataclass(frozen=True, slots=True)
class ValueBridgeEntry:
    driver_key: str
    current_value: Decimal
    prior_value: Decimal
    delta_value: Decimal


@dataclass(frozen=True, slots=True)
class AttributionDetail:
    val_run_id: int
    rows: list[AttributionRow]
    value_bridge: list[ValueBridgeEntry]


@dataclass(frozen=True, slots=True)
class SensitivityRow:
    sensitivity_id: int
    model_output_id: int
    assumption_key: str
    shock_label: str
    shock_value: Decimal
    result_target_price: Decimal
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IncidentSummary:
    incident_id: int
    incident_status: str
    impact_level: str
    opened_at: datetime
    assigned_to: str | None
    severity: str
    domain: str


@dataclass(frozen=True, slots=True)
class IncidentListResponse:
    items: list[IncidentSummary]
    pagination: PaginationMeta


@dataclass(frozen=True, slots=True)
class DqResultDetail:
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


@dataclass(frozen=True, slots=True)
class IncidentDetail:
    incident_id: int
    incident_status: str
    impact_level: str
    assigned_to: str | None
    opened_at: datetime
    resolved_at: datetime | None
    resolution_notes: str | None
    dq_result: DqResultDetail


@dataclass(frozen=True, slots=True)
class IncidentResolution:
    incident_id: int
    override_id: int
    incident_status: str
    resolution_notes: str


@dataclass(frozen=True, slots=True)
class PipelineRun:
    pipeline_run_id: int
    run_uuid: UUID
    pipeline_name: str
    run_mode: str
    triggered_by: str
    status: str
    started_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True, slots=True)
class PipelineRunListResponse:
    items: list[PipelineRun]


@dataclass(frozen=True, slots=True)
class JobEvent:
    job_event_id: int
    job_name: str
    event_type: str
    event_ts: datetime
    message: str | None
    metrics_payload: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class PipelineRunDetail(PipelineRun):
    run_context: dict[str, object] | None
    sla_breach: bool
    job_events: list[JobEvent]
