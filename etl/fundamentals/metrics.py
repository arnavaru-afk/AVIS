"""Derived fundamentals metric builder for annual AVIS statement facts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from avis.db.models import FundMetricFact, FundStatementFact
from etl.fundamentals.parser import (
    CAPEX,
    EBIT,
    EBITDA,
    FREE_CASH_FLOW,
    GROSS_PROFIT,
    NET_INCOME,
    TOTAL_DEBT,
    TOTAL_EQUITY,
    TOTAL_REVENUE,
)

CURRENT_RATIO = "CURRENT_RATIO"
QUICK_RATIO = "QUICK_RATIO"
GROSS_MARGIN = "GROSS_MARGIN"
EBITDA_MARGIN = "EBITDA_MARGIN"
EBIT_MARGIN = "EBIT_MARGIN"
NET_MARGIN = "NET_MARGIN"
ROE = "ROE"
DEBT_TO_EQUITY = "DEBT_TO_EQUITY"
REVENUE_GROWTH_YOY = "REVENUE_GROWTH_YOY"
FCF_MARGIN = "FCF_MARGIN"
CAPEX_INTENSITY = "CAPEX_INTENSITY"
NEGATIVE_DENOMINATOR = "NEGATIVE_DENOMINATOR"
CALC_VERSION = "fundamentals_v1"


@dataclass(frozen=True, slots=True)
class DerivedMetricRow:
    instrument_id: int
    metric_code: str
    metric_value: Decimal | None
    metric_unit: str
    as_of_date: date
    lookback_period: str | None
    calc_version: str
    input_hash: str
    quality_flag: str | None
    created_at: datetime


class FundamentalsMetricBuilder:
    """Build the approved derived metrics from normalized annual facts."""

    def __init__(self, session: Session, *, now_provider: Callable[[], datetime] | None = None) -> None:
        self._session = session
        self._now_provider = now_provider or _utc_now

    def build_rows(
        self,
        *,
        instrument_id: int,
        statement_facts: Iterable[FundStatementFact],
        info: Mapping[str, Any] | None = None,
    ) -> list[DerivedMetricRow]:
        grouped = _group_statement_values(statement_facts)
        ordered_periods = sorted(grouped)
        latest_period = ordered_periods[-1] if ordered_periods else None
        info = info or {}

        rows: list[DerivedMetricRow] = []
        previous_revenue: Decimal | None = None
        for metric_date in ordered_periods:
            facts = grouped[metric_date]
            revenue = _value_for(facts, TOTAL_REVENUE, "Revenue")
            gross_profit = _value_for(facts, GROSS_PROFIT)
            ebitda = _value_for(facts, EBITDA)
            ebit = _value_for(facts, EBIT)
            net_income = _value_for(facts, NET_INCOME)
            total_equity = _value_for(facts, TOTAL_EQUITY)
            total_debt = _value_for(facts, TOTAL_DEBT)
            free_cash_flow = _value_for(facts, FREE_CASH_FLOW)
            capex = _value_for(facts, CAPEX)

            rows.extend(
                [
                    self._ratio_metric(instrument_id, GROSS_MARGIN, gross_profit, revenue, metric_date),
                    self._ratio_metric(instrument_id, EBITDA_MARGIN, ebitda, revenue, metric_date),
                    self._ratio_metric(instrument_id, EBIT_MARGIN, ebit, revenue, metric_date),
                    self._ratio_metric(instrument_id, NET_MARGIN, net_income, revenue, metric_date),
                    self._ratio_metric(instrument_id, ROE, net_income, total_equity, metric_date),
                    self._ratio_metric(instrument_id, DEBT_TO_EQUITY, total_debt, total_equity, metric_date),
                    self._ratio_metric(instrument_id, FCF_MARGIN, free_cash_flow, revenue, metric_date),
                    self._ratio_metric(instrument_id, CAPEX_INTENSITY, capex, revenue, metric_date),
                ]
            )

            yoy = self._growth_metric(
                instrument_id=instrument_id,
                metric_date=metric_date,
                current_value=revenue,
                prior_value=previous_revenue,
            )
            rows.append(yoy)
            previous_revenue = revenue if revenue is not None else previous_revenue

            if metric_date == latest_period:
                rows.append(self._info_metric(instrument_id, CURRENT_RATIO, info.get("currentRatio"), metric_date))
                rows.append(self._info_metric(instrument_id, QUICK_RATIO, info.get("quickRatio"), metric_date))

        return rows

    def upsert_rows(self, rows: Iterable[DerivedMetricRow]) -> int:
        stored = 0
        for row in rows:
            if row.metric_value is None:
                continue
            existing = self._session.scalar(
                select(FundMetricFact).where(
                    FundMetricFact.instrument_id == row.instrument_id,
                    FundMetricFact.metric_code == row.metric_code,
                    FundMetricFact.as_of_date == row.as_of_date,
                    FundMetricFact.calc_version == row.calc_version,
                )
            )
            if existing is None:
                existing = FundMetricFact(
                    instrument_id=row.instrument_id,
                    metric_code=row.metric_code,
                    metric_value=row.metric_value,
                    metric_unit=row.metric_unit,
                    as_of_date=row.as_of_date,
                    lookback_period=row.lookback_period,
                    calc_version=row.calc_version,
                    input_hash=row.input_hash,
                    created_at=row.created_at,
                )
                _assign_pk_if_sqlite(self._session, existing, "metric_fact_id", FundMetricFact)
                self._session.add(existing)
            else:
                existing.metric_value = row.metric_value
                existing.metric_unit = row.metric_unit
                existing.lookback_period = row.lookback_period
                existing.input_hash = row.input_hash
            stored += 1
        self._session.flush()
        return stored

    def _ratio_metric(
        self,
        instrument_id: int,
        metric_code: str,
        numerator: Decimal | None,
        denominator: Decimal | None,
        metric_date: date,
    ) -> DerivedMetricRow:
        quality_flag: str | None = None
        metric_value: Decimal | None
        if numerator is None or denominator is None or denominator == 0:
            metric_value = None
        else:
            if denominator < 0:
                quality_flag = NEGATIVE_DENOMINATOR
            metric_value = numerator / denominator
        return self._build_row(
            instrument_id=instrument_id,
            metric_code=metric_code,
            metric_value=metric_value,
            metric_date=metric_date,
            quality_flag=quality_flag,
        )

    def _growth_metric(
        self,
        *,
        instrument_id: int,
        metric_date: date,
        current_value: Decimal | None,
        prior_value: Decimal | None,
    ) -> DerivedMetricRow:
        if current_value is None or prior_value is None or prior_value == 0:
            metric_value = None
        else:
            metric_value = (current_value - prior_value) / abs(prior_value)
        return self._build_row(
            instrument_id=instrument_id,
            metric_code=REVENUE_GROWTH_YOY,
            metric_value=metric_value,
            metric_date=metric_date,
            quality_flag=None,
        )

    def _info_metric(
        self,
        instrument_id: int,
        metric_code: str,
        raw_value: Any,
        metric_date: date,
    ) -> DerivedMetricRow:
        metric_value = None if raw_value is None else Decimal(str(raw_value))
        return self._build_row(
            instrument_id=instrument_id,
            metric_code=metric_code,
            metric_value=metric_value,
            metric_date=metric_date,
            quality_flag=None,
        )

    def _build_row(
        self,
        *,
        instrument_id: int,
        metric_code: str,
        metric_value: Decimal | None,
        metric_date: date,
        quality_flag: str | None,
    ) -> DerivedMetricRow:
        hash_payload = {
            "instrument_id": instrument_id,
            "metric_code": metric_code,
            "metric_value": None if metric_value is None else str(metric_value),
            "metric_date": metric_date.isoformat(),
            "quality_flag": quality_flag,
        }
        return DerivedMetricRow(
            instrument_id=instrument_id,
            metric_code=metric_code,
            metric_value=metric_value,
            metric_unit="ratio",
            as_of_date=metric_date,
            lookback_period=quality_flag,
            calc_version=CALC_VERSION,
            input_hash=hashlib.sha256(json.dumps(hash_payload, sort_keys=True).encode("utf-8")).hexdigest(),
            quality_flag=quality_flag,
            created_at=self._now_provider(),
        )


def _group_statement_values(statement_facts: Iterable[FundStatementFact]) -> dict[date, dict[str, Decimal]]:
    grouped: dict[date, dict[str, Decimal]] = {}
    for fact in statement_facts:
        metric_date = fact.as_reported_ts.date()
        grouped.setdefault(metric_date, {})[fact.line_item_code] = Decimal(fact.value)
    return grouped


def _value_for(values: Mapping[str, Decimal], *codes: str) -> Decimal | None:
    for code in codes:
        value = values.get(code)
        if value is not None:
            return value
    return None


def _assign_pk_if_sqlite(session: Session, instance: Any, pk_name: str, model: type[Any]) -> None:
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "sqlite":
        return
    if getattr(instance, pk_name, None) is not None:
        return
    current_max = session.scalar(select(func.max(getattr(model, pk_name))))
    setattr(instance, pk_name, 1 if current_max is None else int(current_max) + 1)


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
