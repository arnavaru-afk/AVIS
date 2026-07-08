"""Valuation run orchestration and persistence."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from avis.core.valuation_engine.assumptions import AssumptionSetManager, DcfAssumptions
from avis.core.valuation_engine.confidence import ConfidenceInput, ConfidenceScoreResult, ConfidenceScoringEngine
from avis.core.valuation_engine.dcf import DcfEngine, DcfProjectionInput, DcfResult
from avis.core.valuation_engine.relative import (
    InsufficientPeerSetError,
    RelativeEngine,
    RelativePeerPoint,
    RelativeValuationResult,
)
from avis.db.models import (
    RefInstrument,
    ValAttribution,
    ValConfidenceSnapshot,
    ValModelOutput,
    ValRun,
)


@dataclass(frozen=True, slots=True)
class RelativeInputBundle:
    target_instrument_id: int
    as_of_date: datetime
    target_metrics: RelativePeerPoint


@dataclass(frozen=True, slots=True)
class ValuationRunResult:
    val_run_id: int
    dcf_result: DcfResult
    relative_result: RelativeValuationResult | None
    confidence_result: ConfidenceScoreResult
    blended_target_price: Decimal
    analyst_override_required: bool


class ValuationOrchestrator:
    """Runs DCF + relative valuation and stores a reproducible run snapshot."""

    MODEL_VERSION = "v1"
    CONFIDENCE_VERSION = "v1"

    def __init__(
        self,
        *,
        session: Session,
        dcf_engine: DcfEngine,
        relative_engine: RelativeEngine,
        confidence_engine: ConfidenceScoringEngine,
    ) -> None:
        self._session = session
        self._dcf_engine = dcf_engine
        self._relative_engine = relative_engine
        self._confidence_engine = confidence_engine
        self._assumption_manager = AssumptionSetManager(session)

    def run(
        self,
        *,
        instrument_id: int,
        as_of_ts: datetime,
        dcf_assumptions: DcfAssumptions,
        dcf_projection_input: DcfProjectionInput,
        relative_inputs: RelativeInputBundle,
        data_quality_score: Decimal,
        assumption_stability: Decimal,
        trigger_type: str = "MANUAL",
        pipeline_run_id: int | None = None,
        currency_code: str = "INR",
    ) -> ValuationRunResult:
        self._ensure_instrument_exists(instrument_id)
        val_run = self._create_val_run(
            instrument_id=instrument_id,
            as_of_ts=as_of_ts,
            trigger_type=trigger_type,
            pipeline_run_id=pipeline_run_id,
        )
        self._assumption_manager.store_dcf_assumptions(val_run_id=val_run.val_run_id, assumptions=dcf_assumptions)
        return self._execute_run(
            val_run=val_run,
            as_of_ts=as_of_ts,
            dcf_assumptions=dcf_assumptions,
            dcf_projection_input=dcf_projection_input,
            relative_inputs=relative_inputs,
            data_quality_score=data_quality_score,
            assumption_stability=assumption_stability,
            currency_code=currency_code,
        )

    def run_existing(
        self,
        *,
        val_run_id: int,
        instrument_id: int,
        as_of_ts: datetime,
        dcf_assumptions: DcfAssumptions,
        dcf_projection_input: DcfProjectionInput,
        relative_inputs: RelativeInputBundle,
        data_quality_score: Decimal,
        assumption_stability: Decimal,
        currency_code: str = "INR",
    ) -> ValuationRunResult:
        self._ensure_instrument_exists(instrument_id)
        val_run = self._session.get(ValRun, val_run_id)
        if val_run is None:
            raise ValueError(f"Unknown val_run_id={val_run_id}")
        if val_run.instrument_id != instrument_id:
            raise ValueError(
                f"Run instrument mismatch for val_run_id={val_run_id}: expected {val_run.instrument_id}, received {instrument_id}"
            )
        val_run.as_of_ts = _db_datetime(as_of_ts)
        return self._execute_run(
            val_run=val_run,
            as_of_ts=as_of_ts,
            dcf_assumptions=dcf_assumptions,
            dcf_projection_input=dcf_projection_input,
            relative_inputs=relative_inputs,
            data_quality_score=data_quality_score,
            assumption_stability=assumption_stability,
            currency_code=currency_code,
        )

    def _execute_run(
        self,
        *,
        val_run: ValRun,
        as_of_ts: datetime,
        dcf_assumptions: DcfAssumptions,
        dcf_projection_input: DcfProjectionInput,
        relative_inputs: RelativeInputBundle,
        data_quality_score: Decimal,
        assumption_stability: Decimal,
        currency_code: str,
    ) -> ValuationRunResult:
        val_run.status = "RUNNING"
        val_run.completed_at = None
        val_run.as_of_ts = _db_datetime(as_of_ts)
        self._clear_existing_run_artifacts(val_run.val_run_id)
        self._session.flush()

        dcf_result = self._dcf_engine.run(assumptions=dcf_assumptions, projection_input=dcf_projection_input)
        try:
            relative_result = self._relative_engine.run(
                target_instrument_id=relative_inputs.target_instrument_id,
                as_of_date=relative_inputs.as_of_date.date(),
                target_metrics=relative_inputs.target_metrics,
            )
        except InsufficientPeerSetError as exc:
            relative_result = None
            relative_failure_reason = str(exc)
        else:
            relative_failure_reason = None

        if relative_result is None:
            model_agreement = Decimal("0")
            peer_count = 0
        else:
            model_agreement = self._model_agreement_score(
                dcf_target_price=dcf_result.intrinsic_value_per_share,
                relative_target_price=relative_result.point_estimate_target_price,
            )
            peer_count = max(result.peer_count for result in relative_result.multiple_results)
        confidence_result = self._confidence_engine.score(
            ConfidenceInput(
                data_quality_score=data_quality_score,
                peer_count=peer_count,
                assumption_stability=assumption_stability,
                model_agreement=model_agreement,
            )
        )

        if relative_result is None:
            blended_target_price = dcf_result.intrinsic_value_per_share
            blended_equity_value = dcf_result.equity_value
            blended_enterprise_value = dcf_result.enterprise_value
        else:
            blended_target_price = (
                dcf_result.intrinsic_value_per_share + relative_result.point_estimate_target_price
            ) / Decimal("2")
            blended_equity_value = (dcf_result.equity_value + relative_result.point_estimate_equity_value) / Decimal("2")
            blended_enterprise_value = (
                dcf_result.enterprise_value + relative_result.point_estimate_enterprise_value
            ) / Decimal("2")

        self._store_model_output(
            val_run_id=val_run.val_run_id,
            model_name="DCF",
            currency_code=currency_code,
            equity_value=dcf_result.equity_value,
            enterprise_value=dcf_result.enterprise_value,
            target_price=dcf_result.intrinsic_value_per_share,
            weight=Decimal("0.5"),
            output_payload={
                "inputs": _serialize_input_bundle(dcf_projection_input),
                "assumptions": {key: str(value) for key, value in dcf_assumptions.as_mapping().items()},
                "result": dcf_result.as_payload(),
            },
        )
        if relative_result is None:
            self._store_model_output(
                val_run_id=val_run.val_run_id,
                model_name="RELATIVE",
                currency_code=currency_code,
                equity_value=None,
                enterprise_value=None,
                target_price=None,
                weight=Decimal("0.5"),
                output_payload={
                    "inputs": _serialize_input_bundle(relative_inputs),
                    "status": "UNAVAILABLE",
                    "reason": relative_failure_reason,
                },
            )
        else:
            self._store_model_output(
                val_run_id=val_run.val_run_id,
                model_name="RELATIVE",
                currency_code=currency_code,
                equity_value=relative_result.point_estimate_equity_value,
                enterprise_value=relative_result.point_estimate_enterprise_value,
                target_price=relative_result.point_estimate_target_price,
                weight=Decimal("0.5"),
                output_payload={
                    "inputs": _serialize_input_bundle(relative_inputs),
                    "result": relative_result.as_payload(),
                },
            )
        self._store_model_output(
            val_run_id=val_run.val_run_id,
            model_name="BLENDED",
            currency_code=currency_code,
            equity_value=blended_equity_value,
            enterprise_value=blended_enterprise_value,
            target_price=blended_target_price,
            weight=Decimal("1"),
            output_payload={
                "inputs_reconstructable": True,
                "dcf_target_price": str(dcf_result.intrinsic_value_per_share),
                "relative_target_price": (
                    str(relative_result.point_estimate_target_price) if relative_result is not None else None
                ),
                "confidence": confidence_result.as_payload(),
                "analyst_override_required": confidence_result.analyst_override_required,
            },
        )

        self._store_dcf_attribution(val_run_id=val_run.val_run_id, dcf_result=dcf_result)
        self._store_confidence_snapshot(val_run_id=val_run.val_run_id, confidence_result=confidence_result)
        if confidence_result.analyst_override_required:
            self._store_questionnaire_flag(val_run_id=val_run.val_run_id, confidence_result=confidence_result)

        val_run.status = "OVERRIDDEN" if confidence_result.analyst_override_required else "SUCCESS"
        val_run.completed_at = _db_now()
        self._session.flush()
        return ValuationRunResult(
            val_run_id=val_run.val_run_id,
            dcf_result=dcf_result,
            relative_result=relative_result,
            confidence_result=confidence_result,
            blended_target_price=blended_target_price,
            analyst_override_required=confidence_result.analyst_override_required,
        )

    def reconstruct_inputs(self, *, val_run_id: int) -> dict[str, Any]:
        assumptions = self._assumption_manager.load_dcf_assumptions(val_run_id=val_run_id)
        outputs = list(
            self._session.scalars(
                select(ValModelOutput).where(ValModelOutput.val_run_id == val_run_id)
            )
        )
        return {
            "assumptions": {key: str(value) for key, value in assumptions.as_mapping().items()},
            "model_outputs": [
                {
                    "model_name": output.model_name,
                    "output_payload": output.output_payload,
                }
                for output in outputs
            ],
        }

    def _create_val_run(
        self,
        *,
        instrument_id: int,
        as_of_ts: datetime,
        trigger_type: str,
        pipeline_run_id: int | None,
    ) -> ValRun:
        run = ValRun(
            instrument_id=instrument_id,
            run_uuid=uuid.uuid4(),
            as_of_ts=_db_datetime(as_of_ts),
            run_type="SCHEDULED",
            trigger_type=trigger_type,
            status="RUNNING",
            pipeline_run_id=pipeline_run_id,
            created_at=_db_now(),
            completed_at=None,
        )
        _assign_pk_if_sqlite(self._session, run, "val_run_id", ValRun)
        self._session.add(run)
        self._session.flush()
        return run

    def _store_model_output(
        self,
        *,
        val_run_id: int,
        model_name: str,
        currency_code: str,
        equity_value: Decimal | None,
        enterprise_value: Decimal | None,
        target_price: Decimal | None,
        weight: Decimal,
        output_payload: dict[str, Any],
    ) -> ValModelOutput:
        row = ValModelOutput(
            val_run_id=val_run_id,
            model_name=model_name,
            model_version=self.MODEL_VERSION,
            equity_value=equity_value,
            enterprise_value=enterprise_value,
            target_price=target_price,
            weight=weight,
            currency_code=currency_code,
            output_payload=output_payload,
            created_at=_db_now(),
        )
        _assign_pk_if_sqlite(self._session, row, "model_output_id", ValModelOutput)
        self._session.add(row)
        self._session.flush()
        return row

    def _store_dcf_attribution(self, *, val_run_id: int, dcf_result: DcfResult) -> None:
        for line in dcf_result.projection_lines:
            row = ValAttribution(
                val_run_id=val_run_id,
                driver_type="DCF_FCF",
                driver_key=f"YEAR_{line.year_index}",
                impact_value_abs=line.free_cash_flow,
                impact_value_pct=None,
                direction=_direction(line.free_cash_flow),
                evidence_ref={
                    "revenue": str(line.revenue),
                    "ebit": str(line.ebit),
                    "nopat": str(line.nopat),
                    "depreciation_and_amortization": str(line.depreciation_and_amortization),
                    "capex": str(line.capex),
                    "delta_nwc": str(line.delta_nwc),
                    "present_value": str(line.present_value),
                },
                created_at=_db_now(),
            )
            _assign_pk_if_sqlite(self._session, row, "attribution_id", ValAttribution)
            self._session.add(row)
        terminal_row = ValAttribution(
            val_run_id=val_run_id,
            driver_type="DCF_TERMINAL_VALUE",
            driver_key="TERMINAL_VALUE",
            impact_value_abs=dcf_result.terminal_present_value,
            impact_value_pct=None,
            direction=_direction(dcf_result.terminal_present_value),
            evidence_ref={
                "terminal_value": str(dcf_result.terminal_value),
                "terminal_present_value": str(dcf_result.terminal_present_value),
            },
            created_at=_db_now(),
        )
        _assign_pk_if_sqlite(self._session, terminal_row, "attribution_id", ValAttribution)
        self._session.add(terminal_row)
        self._session.flush()

    def _store_confidence_snapshot(
        self,
        *,
        val_run_id: int,
        confidence_result: ConfidenceScoreResult,
    ) -> ValConfidenceSnapshot:
        row = ValConfidenceSnapshot(
            val_run_id=val_run_id,
            overall_confidence=confidence_result.composite_score,
            data_quality_score=confidence_result.data_quality_score,
            management_credibility_score=confidence_result.assumption_stability_score,
            industry_stability_score=confidence_result.peer_count_score,
            forecast_accuracy_score=confidence_result.assumption_stability_score,
            news_reliability_score=confidence_result.data_quality_score,
            model_agreement_score=confidence_result.model_agreement_score,
            scenario_dispersion_score=(Decimal("100") - confidence_result.model_agreement_score).quantize(
                Decimal("0.01")
            ),
            scoring_version=self.CONFIDENCE_VERSION,
            created_at=_db_now(),
        )
        _assign_pk_if_sqlite(self._session, row, "confidence_id", ValConfidenceSnapshot)
        self._session.add(row)
        self._session.flush()
        return row

    def _store_questionnaire_flag(
        self,
        *,
        val_run_id: int,
        confidence_result: ConfidenceScoreResult,
    ) -> None:
        row = ValAttribution(
            val_run_id=val_run_id,
            driver_type="CONFIDENCE",
            driver_key="ANALYST_OVERRIDE_REQUIRED",
            impact_value_abs=confidence_result.composite_score,
            impact_value_pct=None,
            direction="DOWN",
            evidence_ref=confidence_result.as_payload(),
            created_at=_db_now(),
        )
        _assign_pk_if_sqlite(self._session, row, "attribution_id", ValAttribution)
        self._session.add(row)
        self._session.flush()

    def _ensure_instrument_exists(self, instrument_id: int) -> None:
        if self._session.get(RefInstrument, instrument_id) is None:
            raise ValueError(f"Unknown instrument_id={instrument_id}")

    def _clear_existing_run_artifacts(self, val_run_id: int) -> None:
        for row in list(
            self._session.scalars(
                select(ValModelOutput).where(ValModelOutput.val_run_id == val_run_id)
            )
        ):
            self._session.delete(row)
        for row in list(
            self._session.scalars(
                select(ValAttribution).where(ValAttribution.val_run_id == val_run_id)
            )
        ):
            self._session.delete(row)
        for row in list(
            self._session.scalars(
                select(ValConfidenceSnapshot).where(ValConfidenceSnapshot.val_run_id == val_run_id)
            )
        ):
            self._session.delete(row)
        self._session.flush()

    @staticmethod
    def _model_agreement_score(*, dcf_target_price: Decimal, relative_target_price: Decimal) -> Decimal:
        denominator = (abs(dcf_target_price) + abs(relative_target_price)) / Decimal("2")
        if denominator == 0:
            return Decimal("100")
        spread_pct = abs(dcf_target_price - relative_target_price) / denominator
        score = Decimal("100") - min(Decimal("100"), spread_pct * Decimal("100"))
        return score.quantize(Decimal("0.01"))


def _serialize_input_bundle(bundle: Any) -> dict[str, Any]:
    if hasattr(bundle, "__dict__"):
        raw = vars(bundle)
    else:
        raw = {
            key: getattr(bundle, key)
            for key in bundle.__dataclass_fields__.keys()  # type: ignore[attr-defined]
        }
    payload: dict[str, Any] = {}
    for key, value in raw.items():
        payload[key] = _serialize_value(value)
    return payload


def _serialize_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_serialize_value(item) for item in value]
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize_value(item) for key, item in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return _serialize_input_bundle(value)
    if isinstance(value, (Decimal, uuid.UUID)):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _direction(value: Decimal) -> str:
    if value > 0:
        return "UP"
    if value < 0:
        return "DOWN"
    return "NEUTRAL"


def _assign_pk_if_sqlite(session: Session, instance: Any, pk_name: str, model: type[Any]) -> None:
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "sqlite":
        return
    if getattr(instance, pk_name, None) is not None:
        return
    pk_column = getattr(model, pk_name)
    current_max = session.scalar(select(func.max(pk_column)))
    setattr(instance, pk_name, 1 if current_max is None else int(current_max) + 1)


def _db_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _db_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
