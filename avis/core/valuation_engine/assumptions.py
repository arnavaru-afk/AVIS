"""Assumption set lifecycle management for valuation runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from avis.db.models import ValAssumptionSet, ValRun

REQUIRED_DCF_KEYS = (
    "wacc",
    "terminal_growth",
    "forecast_years",
    "revenue_cagr",
    "ebit_margin",
)


class ImmutableAssumptionSetError(RuntimeError):
    """Raised when a referenced assumption set is edited."""


@dataclass(frozen=True, slots=True)
class DcfAssumptions:
    """Minimal DCF assumption schema approved for v1."""

    wacc: Decimal
    terminal_growth: Decimal
    forecast_years: int
    revenue_cagr: Decimal
    ebit_margin: Decimal
    version: int = 1

    def as_mapping(self) -> dict[str, Decimal | int]:
        return {
            "wacc": self.wacc,
            "terminal_growth": self.terminal_growth,
            "forecast_years": self.forecast_years,
            "revenue_cagr": self.revenue_cagr,
            "ebit_margin": self.ebit_margin,
        }


class AssumptionSetManager:
    """Creates immutable assumption snapshots tied to valuation runs."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def store_dcf_assumptions(
        self,
        *,
        val_run_id: int,
        assumptions: DcfAssumptions,
        source_type: str = "ANALYST",
        source_reference: str | None = None,
    ) -> list[ValAssumptionSet]:
        self._ensure_val_run_exists(val_run_id)
        self._validate_required_fields(assumptions.as_mapping())
        if self._has_assumptions(val_run_id):
            raise ImmutableAssumptionSetError(
                f"Assumption set for val_run_id={val_run_id} already exists and is immutable"
            )

        rows: list[ValAssumptionSet] = []
        reference = source_reference or f"assumption_set:v{assumptions.version}"
        for key, value in assumptions.as_mapping().items():
            numeric_value = Decimal(str(value)) if key != "forecast_years" else Decimal(int(value))
            row = ValAssumptionSet(
                val_run_id=val_run_id,
                assumption_key=key.upper(),
                assumption_value_numeric=numeric_value,
                assumption_value_text=None,
                assumption_unit="RATIO" if key != "forecast_years" else "YEARS",
                source_type=source_type,
                source_reference=reference,
                created_at=_db_now(),
            )
            _assign_pk_if_sqlite(self._session, row, "assumption_set_id", ValAssumptionSet)
            self._session.add(row)
            rows.append(row)
        self._session.flush()
        return rows

    def load_dcf_assumptions(self, *, val_run_id: int) -> DcfAssumptions:
        rows = list(
            self._session.scalars(
                select(ValAssumptionSet).where(ValAssumptionSet.val_run_id == val_run_id)
            )
        )
        if not rows:
            raise ValueError(f"No assumptions stored for val_run_id={val_run_id}")

        mapping: dict[str, Decimal] = {}
        for row in rows:
            if row.assumption_value_numeric is not None:
                mapping[row.assumption_key.lower()] = Decimal(row.assumption_value_numeric)
        self._validate_required_fields(mapping)
        return DcfAssumptions(
            wacc=mapping["wacc"],
            terminal_growth=mapping["terminal_growth"],
            forecast_years=int(mapping["forecast_years"]),
            revenue_cagr=mapping["revenue_cagr"],
            ebit_margin=mapping["ebit_margin"],
        )

    def update_assumption(
        self,
        *,
        assumption_set_id: int,
        numeric_value: Decimal | None = None,
        text_value: str | None = None,
    ) -> None:
        row = self._session.get(ValAssumptionSet, assumption_set_id)
        if row is None:
            raise ValueError(f"Unknown assumption_set_id={assumption_set_id}")
        raise ImmutableAssumptionSetError(
            f"Assumption set {assumption_set_id} is immutable once linked to val_run_id={row.val_run_id}"
        )

    def _ensure_val_run_exists(self, val_run_id: int) -> None:
        if self._session.get(ValRun, val_run_id) is None:
            raise ValueError(f"Unknown val_run_id={val_run_id}")

    def _has_assumptions(self, val_run_id: int) -> bool:
        return (
            self._session.scalar(
                select(func.count())
                .select_from(ValAssumptionSet)
                .where(ValAssumptionSet.val_run_id == val_run_id)
            )
            or 0
        ) > 0

    @staticmethod
    def _validate_required_fields(values: dict[str, Any]) -> None:
        missing = [key for key in REQUIRED_DCF_KEYS if key not in values or values[key] is None]
        if missing:
            raise ValueError(f"Missing required DCF assumption fields: {', '.join(missing)}")


def _assign_pk_if_sqlite(session: Session, instance: Any, pk_name: str, model: type[Any]) -> None:
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "sqlite":
        return
    if getattr(instance, pk_name, None) is not None:
        return
    pk_column = getattr(model, pk_name)
    current_max = session.scalar(select(func.max(pk_column)))
    setattr(instance, pk_name, 1 if current_max is None else int(current_max) + 1)


def _db_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
