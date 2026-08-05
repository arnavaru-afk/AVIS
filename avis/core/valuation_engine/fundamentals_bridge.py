"""Point-in-time bridge from stored fundamentals to DCF projection inputs."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from avis.core.valuation_engine.dcf import DcfProjectionInput
from avis.db.models import FundStatementFact
from etl.fundamentals.parser import (
    CAPEX,
    CASH,
    DEPRECIATION,
    SHARES_OUTSTANDING,
    TOTAL_DEBT,
    TOTAL_REVENUE,
)

LEGACY_LINE_ITEM_ALIASES: dict[str, tuple[str, ...]] = {
    TOTAL_REVENUE: ("Revenue",),
}


class FundamentalsBridge:
    """Build reproducible DCF inputs from the most recent PIT-safe annual facts."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def build_projection_input(
        self,
        *,
        instrument_id: int,
        as_of_date: date,
        forecast_years: int,
        wacc: Decimal,
        revenue_cagr: Decimal,
        ebit_margin: Decimal,
        tax_rate: Decimal = Decimal("0.25"),
    ) -> DcfProjectionInput | None:
        del wacc, revenue_cagr, ebit_margin
        period_end_ts = datetime.combine(as_of_date, time.max, tzinfo=timezone.utc).replace(tzinfo=None)
        candidate_periods = [
            row[0]
            for row in self._session.execute(
                select(FundStatementFact.as_reported_ts)
                .where(
                    FundStatementFact.instrument_id == instrument_id,
                    FundStatementFact.fiscal_period == "FY",
                    FundStatementFact.as_reported_ts <= period_end_ts,
                )
                .distinct()
                .order_by(FundStatementFact.as_reported_ts.desc())
            )
        ]
        if not candidate_periods:
            return None

        latest_period = candidate_periods[0]
        rows = list(
            self._session.scalars(
                select(FundStatementFact).where(
                    FundStatementFact.instrument_id == instrument_id,
                    FundStatementFact.fiscal_period == "FY",
                    FundStatementFact.as_reported_ts == latest_period,
                )
            )
        )
        if not rows:
            return None

        values = {row.line_item_code: Decimal(row.value) for row in rows}
        revenue_base = _value_for(values, TOTAL_REVENUE, *LEGACY_LINE_ITEM_ALIASES.get(TOTAL_REVENUE, ()))
        depreciation = _value_for(values, DEPRECIATION)
        capex = _value_for(values, CAPEX)
        total_debt = _value_for(values, TOTAL_DEBT)
        cash = _value_for(values, CASH)
        share_count = _value_for(values, SHARES_OUTSTANDING)
        if None in (revenue_base, depreciation, capex, total_debt, cash, share_count):
            return None
        if share_count is None or share_count <= 0:
            return None
        return DcfProjectionInput(
            revenue_base=revenue_base,
            tax_rate=tax_rate,
            share_count=share_count,
            net_debt=total_debt - cash,
            depreciation_and_amortization=tuple(depreciation for _ in range(forecast_years)),
            capex=tuple(capex for _ in range(forecast_years)),
            delta_nwc=tuple(Decimal("0") for _ in range(forecast_years)),
        )


def _value_for(values: dict[str, Decimal], *codes: str) -> Decimal | None:
    for code in codes:
        value = values.get(code)
        if value is not None:
            return value
    return None
