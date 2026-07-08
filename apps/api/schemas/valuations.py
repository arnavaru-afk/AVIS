from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import Field

from apps.api.schemas import AvisSchema
from apps.api.schemas.instruments import PaginationMeta


class AssumptionSetRequest(AvisSchema):
    wacc: Decimal
    terminal_growth: Decimal
    forecast_years: int = Field(ge=1)
    revenue_cagr: Decimal
    ebit_margin: Decimal
    da: list[Decimal] | None = None
    capex: list[Decimal] | None = None
    nwc_delta: list[Decimal] | None = None


class RunValuationRequest(AvisSchema):
    instrument_uuid: UUID
    assumption_set: AssumptionSetRequest
    run_label: str = Field(min_length=1, max_length=128)


class ValuationRunResponse(AvisSchema):
    val_run_id: int
    run_uuid: UUID
    status: str
    queued_at: datetime
    blended_value: Decimal | None = None
    dcf_value: Decimal | None = None
    relative_value: Decimal | None
    confidence_score: Decimal | None = None
    override_required: bool | None = None


class AssumptionEntryResponse(AvisSchema):
    key: str
    numeric_value: Decimal | None
    text_value: str | None
    unit: str | None
    source_type: str
    source_reference: str | None


class ModelOutputResponse(AvisSchema):
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


class AttributionResponse(AvisSchema):
    attribution_id: int
    driver_type: str
    driver_key: str
    impact_value_abs: Decimal
    impact_value_pct: Decimal | None
    direction: str
    evidence_ref: dict[str, object] | None
    created_at: datetime


class ConfidenceSnapshotResponse(AvisSchema):
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


class ValuationDetailResponse(AvisSchema):
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
    assumption_set: list[AssumptionEntryResponse]
    model_outputs: list[ModelOutputResponse]
    attributions: list[AttributionResponse]
    confidence_snapshots: list[ConfidenceSnapshotResponse]


class ValuationListItem(AvisSchema):
    val_run_id: int
    run_uuid: UUID
    as_of_ts: datetime
    status: str
    run_label: str | None
    blended_value: Decimal | None
    dcf_value: Decimal | None
    relative_value: Decimal | None
    confidence_score: Decimal | None


class ValuationListResponse(AvisSchema):
    items: list[ValuationListItem]
    pagination: PaginationMeta


class ValueBridgeEntry(AvisSchema):
    driver_key: str
    current_value: Decimal
    prior_value: Decimal
    delta_value: Decimal


class AttributionDetailResponse(AvisSchema):
    val_run_id: int
    rows: list[AttributionResponse]
    value_bridge: list[ValueBridgeEntry]


class SensitivityRowResponse(AvisSchema):
    sensitivity_id: int
    model_output_id: int
    assumption_key: str
    shock_label: str
    shock_value: Decimal
    result_target_price: Decimal
    created_at: datetime


class SensitivityResponse(AvisSchema):
    val_run_id: int
    rows: list[SensitivityRowResponse]
