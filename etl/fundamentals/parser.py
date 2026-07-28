"""Parse annual yfinance statements into normalized AVIS fact rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any, Callable, Iterable

import pandas as pd

PL = "PL"
BS = "BS"
CF = "CF"
ANNUAL_PERIOD = "FY"
CURRENCY_CODE = "INR"
SCALE_CODE = "ABS"
SOURCE_SYSTEM = "YFINANCE"

TOTAL_REVENUE = "TOTAL_REVENUE"
GROSS_PROFIT = "GROSS_PROFIT"
EBITDA = "EBITDA"
EBIT = "EBIT"
NET_INCOME = "NET_INCOME"
INTEREST_EXPENSE = "INTEREST_EXPENSE"
TAX_PROVISION = "TAX_PROVISION"
DILUTED_EPS = "DILUTED_EPS"
TOTAL_ASSETS = "TOTAL_ASSETS"
TOTAL_LIABILITIES = "TOTAL_LIABILITIES"
TOTAL_EQUITY = "TOTAL_EQUITY"
TOTAL_DEBT = "TOTAL_DEBT"
NET_DEBT = "NET_DEBT"
CASH = "CASH"
ACCOUNTS_RECEIVABLE = "ACCOUNTS_RECEIVABLE"
INVENTORY = "INVENTORY"
ACCOUNTS_PAYABLE = "ACCOUNTS_PAYABLE"
SHARES_OUTSTANDING = "SHARES_OUTSTANDING"
OPERATING_CF = "OPERATING_CF"
CAPEX = "CAPEX"
FREE_CASH_FLOW = "FREE_CASH_FLOW"
DEPRECIATION = "DEPRECIATION"

INCOME_FIELD_MAP: dict[str, tuple[str, ...]] = {
    TOTAL_REVENUE: ("Total Revenue", "Operating Revenue"),
    GROSS_PROFIT: ("Gross Profit",),
    EBITDA: ("Normalized EBITDA",),
    EBIT: ("Operating Income",),
    NET_INCOME: ("Net Income", "Net Income Common Stockholders"),
    INTEREST_EXPENSE: ("Interest Expense",),
    TAX_PROVISION: ("Tax Provision",),
    DILUTED_EPS: ("Diluted EPS",),
}

BALANCE_FIELD_MAP: dict[str, tuple[str, ...]] = {
    TOTAL_ASSETS: ("Total Assets",),
    TOTAL_LIABILITIES: ("Total Liabilities Net Minority Interest",),
    TOTAL_EQUITY: ("Stockholders Equity",),
    TOTAL_DEBT: ("Total Debt",),
    NET_DEBT: ("Net Debt",),
    CASH: ("Cash And Cash Equivalents",),
    ACCOUNTS_RECEIVABLE: ("Accounts Receivable", "Net Receivables"),
    INVENTORY: ("Inventory",),
    ACCOUNTS_PAYABLE: ("Accounts Payable",),
}

CASHFLOW_FIELD_MAP: dict[str, tuple[str, ...]] = {
    OPERATING_CF: ("Operating Cash Flow",),
    CAPEX: ("Capital Expenditure",),
    FREE_CASH_FLOW: ("Free Cash Flow",),
    DEPRECIATION: ("Depreciation And Amortization",),
}


@dataclass(frozen=True, slots=True)
class ParsedStatementFact:
    instrument_id: int
    fiscal_year: int
    fiscal_period: str
    statement_type: str
    line_item_code: str
    value: Decimal | None
    currency_code: str
    scale_code: str
    period_end_date: date
    as_reported_ts: datetime
    effective_from_ts: datetime
    effective_to_ts: datetime | None
    source_system: str
    source_record_id: str | None
    ingested_at: datetime


class YFinanceStatementParser:
    """Convert yfinance annual statement DataFrames into AVIS fact rows."""

    def __init__(self, *, now_provider: Callable[[], datetime] | None = None) -> None:
        self._now_provider = now_provider or _utc_now

    def parse(
        self,
        *,
        instrument_id: int,
        income_stmt: pd.DataFrame | None,
        balance_sheet: pd.DataFrame | None,
        cashflow: pd.DataFrame | None,
        info: dict[str, Any] | None = None,
    ) -> list[ParsedStatementFact]:
        ingested_at = self._now_provider()
        rows: list[ParsedStatementFact] = []
        rows.extend(
            self._parse_statement(
                instrument_id=instrument_id,
                statement_type=PL,
                frame=income_stmt,
                field_map=INCOME_FIELD_MAP,
                ingested_at=ingested_at,
            )
        )
        rows.extend(
            self._parse_statement(
                instrument_id=instrument_id,
                statement_type=BS,
                frame=balance_sheet,
                field_map=BALANCE_FIELD_MAP,
                ingested_at=ingested_at,
            )
        )
        rows.extend(
            self._parse_statement(
                instrument_id=instrument_id,
                statement_type=CF,
                frame=cashflow,
                field_map=CASHFLOW_FIELD_MAP,
                ingested_at=ingested_at,
            )
        )
        rows.extend(
            self._parse_shares_outstanding(
                instrument_id=instrument_id,
                balance_sheet=balance_sheet,
                info=info,
                ingested_at=ingested_at,
            )
        )
        return rows

    def _parse_statement(
        self,
        *,
        instrument_id: int,
        statement_type: str,
        frame: pd.DataFrame | None,
        field_map: dict[str, tuple[str, ...]],
        ingested_at: datetime,
    ) -> list[ParsedStatementFact]:
        if frame is None or frame.empty:
            return []

        rows: list[ParsedStatementFact] = []
        for column in frame.columns:
            period_end_date = _coerce_period_end_date(column)
            ts_value = _period_end_timestamp(period_end_date)
            for line_item_code, labels in field_map.items():
                value = self._extract_value(frame, column=column, labels=labels)
                if line_item_code == CAPEX and value is not None:
                    value = abs(value)
                rows.append(
                    ParsedStatementFact(
                        instrument_id=instrument_id,
                        fiscal_year=period_end_date.year,
                        fiscal_period=ANNUAL_PERIOD,
                        statement_type=statement_type,
                        line_item_code=line_item_code,
                        value=value,
                        currency_code=CURRENCY_CODE,
                        scale_code=SCALE_CODE,
                        period_end_date=period_end_date,
                        as_reported_ts=ts_value,
                        effective_from_ts=ts_value,
                        effective_to_ts=None,
                        source_system=SOURCE_SYSTEM,
                        source_record_id=f"{statement_type}:{period_end_date.isoformat()}:{line_item_code}",
                        ingested_at=ingested_at,
                    )
                )
        return rows

    def _parse_shares_outstanding(
        self,
        *,
        instrument_id: int,
        balance_sheet: pd.DataFrame | None,
        info: dict[str, Any] | None,
        ingested_at: datetime,
    ) -> list[ParsedStatementFact]:
        if balance_sheet is None or balance_sheet.empty:
            return []
        inferred = _infer_shares_outstanding(info or {})
        if inferred is None:
            return []
        latest_period = max(_coerce_period_end_date(column) for column in balance_sheet.columns)
        ts_value = _period_end_timestamp(latest_period)
        return [
            ParsedStatementFact(
                instrument_id=instrument_id,
                fiscal_year=latest_period.year,
                fiscal_period=ANNUAL_PERIOD,
                statement_type=BS,
                line_item_code=SHARES_OUTSTANDING,
                value=inferred,
                currency_code=CURRENCY_CODE,
                scale_code=SCALE_CODE,
                period_end_date=latest_period,
                as_reported_ts=ts_value,
                effective_from_ts=ts_value,
                effective_to_ts=None,
                source_system=SOURCE_SYSTEM,
                source_record_id=f"BS:{latest_period.isoformat()}:{SHARES_OUTSTANDING}",
                ingested_at=ingested_at,
            )
        ]

    @staticmethod
    def _extract_value(
        frame: pd.DataFrame,
        *,
        column: Any,
        labels: Iterable[str],
    ) -> Decimal | None:
        for label in labels:
            if label not in frame.index:
                continue
            raw_value = frame.at[label, column]
            value = _coerce_optional_decimal(raw_value)
            if value is not None:
                return value
        return None


def _infer_shares_outstanding(info: dict[str, Any]) -> Decimal | None:
    market_cap = _coerce_optional_decimal(info.get("marketCap"))
    price = _coerce_optional_decimal(
        info.get("currentPrice")
        or info.get("regularMarketPrice")
        or info.get("previousClose")
    )
    if market_cap is None or price is None or price <= 0:
        return None
    return market_cap / price


def _coerce_period_end_date(value: Any) -> date:
    timestamp = pd.Timestamp(value)
    return timestamp.date()


def _coerce_optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if pd.isna(value):
        return None
    return Decimal(str(value))


def _period_end_timestamp(period_end_date: date) -> datetime:
    return datetime.combine(period_end_date, time.min, tzinfo=UTC).replace(tzinfo=None)


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
