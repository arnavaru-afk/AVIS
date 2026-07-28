from __future__ import annotations

import sys
import uuid
from datetime import date, datetime
from decimal import Decimal
import os
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///./avis_fundamentals_import_guard.db")
os.environ.setdefault("AVIS_JWT_PUBLIC_KEY", "test-public-key")

from apps.api.routers.valuations import process_queued_valuation_run_sync, queue_valuation_run_sync
from apps.api.schemas.valuations import AssumptionSetRequest, RunValuationRequest
from avis.core.valuation_engine.fundamentals_bridge import FundamentalsBridge
from avis.db.base import Base
from avis.db.models import (
    DqIncident,
    DqResult,
    DqRule,
    FundMetricFact,
    FundStatementFact,
    OpsDataLineage,
    OpsJobEvent,
    OpsPipelineRun,
    RefCompany,
    RefExchange,
    RefInstrument,
    ValAssumptionSet,
    ValAttribution,
    ValConfidenceSnapshot,
    ValModelOutput,
    ValRun,
)
from etl.fundamentals.metrics import GROSS_MARGIN, NEGATIVE_DENOMINATOR, FundamentalsMetricBuilder
from etl.fundamentals.parser import (
    CAPEX,
    CASH,
    DEPRECIATION,
    SHARES_OUTSTANDING,
    TOTAL_DEBT,
    TOTAL_EQUITY,
    TOTAL_REVENUE,
    YFinanceStatementParser,
)
from etl.fundamentals.pipeline import FundamentalsPipeline


class FakeTicker:
    def __init__(self, *, income_stmt, balance_sheet, cashflow, info) -> None:
        self.income_stmt = income_stmt
        self.balance_sheet = balance_sheet
        self.cashflow = cashflow
        self.info = info


def test_parser_maps_known_yfinance_labels() -> None:
    parser = YFinanceStatementParser(now_provider=lambda: datetime(2026, 7, 28, 9, 0))
    period = pd.Timestamp("2025-03-31")
    income_stmt = pd.DataFrame(
        {
            period: {
                "Total Revenue": 1000,
                "Gross Profit": 400,
                "Normalized EBITDA": 250,
                "Operating Income": 200,
                "Net Income": 150,
                "Interest Expense": 20,
                "Tax Provision": 45,
                "Diluted EPS": 12.5,
            }
        }
    )
    balance_sheet = pd.DataFrame(
        {
            period: {
                "Total Assets": 3000,
                "Total Liabilities Net Minority Interest": 1200,
                "Stockholders Equity": 1800,
                "Total Debt": 500,
                "Net Debt": 350,
                "Cash And Cash Equivalents": 150,
                "Accounts Receivable": 200,
                "Inventory": 100,
                "Accounts Payable": 90,
            }
        }
    )
    cashflow = pd.DataFrame(
        {
            period: {
                "Operating Cash Flow": 220,
                "Capital Expenditure": -80,
                "Free Cash Flow": 140,
                "Depreciation And Amortization": 60,
            }
        }
    )

    rows = parser.parse(
        instrument_id=101,
        income_stmt=income_stmt,
        balance_sheet=balance_sheet,
        cashflow=cashflow,
        info={"marketCap": 10000, "currentPrice": 100},
    )
    values = {(row.statement_type, row.line_item_code): row.value for row in rows}

    assert values[("PL", TOTAL_REVENUE)] == Decimal("1000")
    assert values[("PL", "GROSS_PROFIT")] == Decimal("400")
    assert values[("CF", CAPEX)] == Decimal("80")
    assert values[("BS", SHARES_OUTSTANDING)] == Decimal("100")


def test_parser_missing_field_yields_null_value() -> None:
    parser = YFinanceStatementParser(now_provider=lambda: datetime(2026, 7, 28, 9, 0))
    period = pd.Timestamp("2025-03-31")
    income_stmt = pd.DataFrame({period: {"Total Revenue": 1000}})

    rows = parser.parse(
        instrument_id=101,
        income_stmt=income_stmt,
        balance_sheet=pd.DataFrame({period: {}}),
        cashflow=pd.DataFrame({period: {}}),
        info={},
    )

    diluted_eps_row = next(
        row
        for row in rows
        if row.statement_type == "PL" and row.line_item_code == "DILUTED_EPS"
    )
    assert diluted_eps_row.value is None


def test_parser_uses_period_end_for_pit_timestamp() -> None:
    parser = YFinanceStatementParser(now_provider=lambda: datetime(2026, 7, 28, 9, 0))
    period = pd.Timestamp("2024-03-31")
    income_stmt = pd.DataFrame({period: {"Total Revenue": 1000}})

    rows = parser.parse(
        instrument_id=101,
        income_stmt=income_stmt,
        balance_sheet=pd.DataFrame({period: {}}),
        cashflow=pd.DataFrame({period: {}}),
        info={},
    )

    revenue_row = next(
        row
        for row in rows
        if row.statement_type == "PL" and row.line_item_code == TOTAL_REVENUE
    )
    assert revenue_row.as_reported_ts.date() == date(2024, 3, 31)
    assert revenue_row.effective_from_ts.date() == date(2024, 3, 31)


def test_metrics_compute_gross_margin() -> None:
    with _session_with_tables() as session:
        builder = FundamentalsMetricBuilder(session, now_provider=lambda: datetime(2026, 7, 28, 9, 0))
        rows = builder.build_rows(
            instrument_id=101,
            statement_facts=[
                _statement_fact(1, 101, TOTAL_REVENUE, "1000", date(2025, 3, 31)),
                _statement_fact(2, 101, "GROSS_PROFIT", "400", date(2025, 3, 31)),
            ],
            info={},
        )

        gross_margin = next(row for row in rows if row.metric_code == GROSS_MARGIN)
        assert gross_margin.metric_value == Decimal("0.4")


def test_metrics_divide_by_zero_yields_null() -> None:
    with _session_with_tables() as session:
        builder = FundamentalsMetricBuilder(session, now_provider=lambda: datetime(2026, 7, 28, 9, 0))
        rows = builder.build_rows(
            instrument_id=101,
            statement_facts=[
                _statement_fact(1, 101, TOTAL_REVENUE, "0", date(2025, 3, 31)),
                _statement_fact(2, 101, "GROSS_PROFIT", "400", date(2025, 3, 31)),
            ],
            info={},
        )

        gross_margin = next(row for row in rows if row.metric_code == GROSS_MARGIN)
        assert gross_margin.metric_value is None


def test_metrics_negative_denominator_sets_quality_flag() -> None:
    with _session_with_tables() as session:
        builder = FundamentalsMetricBuilder(session, now_provider=lambda: datetime(2026, 7, 28, 9, 0))
        rows = builder.build_rows(
            instrument_id=101,
            statement_facts=[
                _statement_fact(1, 101, TOTAL_REVENUE, "1000", date(2025, 3, 31)),
                _statement_fact(2, 101, TOTAL_EQUITY, "-500", date(2025, 3, 31)),
                _statement_fact(3, 101, "NET_INCOME", "100", date(2025, 3, 31)),
            ],
            info={},
        )

        roe = next(row for row in rows if row.metric_code == "ROE")
        assert roe.metric_value == Decimal("-0.2")
        assert roe.quality_flag == NEGATIVE_DENOMINATOR


def test_pipeline_is_idempotent() -> None:
    with _session_with_tables(include_ops=True, include_reference=True) as session:
        _seed_instrument(session)
        pipeline = FundamentalsPipeline(
            session=session,
            ticker_factory=lambda _symbol: _fake_ticker(),
            now_provider=lambda: datetime(2026, 7, 28, 9, 0),
        )

        first = pipeline.run(101, "ITC")
        statement_count_after_first = session.scalar(select(func.count()).select_from(FundStatementFact))
        metric_count_after_first = session.scalar(select(func.count()).select_from(FundMetricFact))

        second = pipeline.run(101, "ITC")
        statement_count_after_second = session.scalar(select(func.count()).select_from(FundStatementFact))
        metric_count_after_second = session.scalar(select(func.count()).select_from(FundMetricFact))

        assert first.skipped is False
        assert second.skipped is False
        assert statement_count_after_first == statement_count_after_second
        assert metric_count_after_first == metric_count_after_second
        assert session.scalar(select(func.count()).select_from(OpsDataLineage)) == 2


def test_pipeline_skips_instrument_without_yfinance_data() -> None:
    with _session_with_tables(include_ops=True, include_reference=True) as session:
        _seed_instrument(session)
        empty_ticker = FakeTicker(
            income_stmt=pd.DataFrame(),
            balance_sheet=pd.DataFrame(),
            cashflow=pd.DataFrame(),
            info={},
        )
        pipeline = FundamentalsPipeline(
            session=session,
            ticker_factory=lambda _symbol: empty_ticker,
            now_provider=lambda: datetime(2026, 7, 28, 9, 0),
        )

        result = pipeline.run(101, "ITC")
        events = list(session.scalars(select(OpsJobEvent).order_by(OpsJobEvent.job_event_id.asc())))

        assert result.skipped is True
        assert "No annual periods" in (result.skip_reason or "")
        assert events[-1].event_type == "ERROR"


def test_bridge_returns_projection_input_from_latest_annual_facts() -> None:
    with _session_with_tables(include_reference=True) as session:
        _seed_instrument(session)
        _seed_bridge_facts(session, period_end=date(2026, 3, 31), revenue="1500")

        projection = FundamentalsBridge(session).build_projection_input(
            instrument_id=101,
            as_of_date=date(2026, 7, 28),
            forecast_years=3,
            wacc=Decimal("0.11"),
            revenue_cagr=Decimal("0.08"),
            ebit_margin=Decimal("0.22"),
        )

        assert projection is not None
        assert projection.revenue_base == Decimal("1500")
        assert projection.net_debt == Decimal("350")
        assert projection.share_count == Decimal("100")
        assert projection.capex == (Decimal("80"), Decimal("80"), Decimal("80"))


def test_bridge_strict_pit_returns_none_before_any_data() -> None:
    with _session_with_tables(include_reference=True) as session:
        _seed_instrument(session)
        _seed_bridge_facts(session, period_end=date(2026, 3, 31), revenue="1500")

        projection = FundamentalsBridge(session).build_projection_input(
            instrument_id=101,
            as_of_date=date(2025, 12, 31),
            forecast_years=3,
            wacc=Decimal("0.11"),
            revenue_cagr=Decimal("0.08"),
            ebit_margin=Decimal("0.22"),
        )

        assert projection is None


def test_bridge_selects_most_recent_period_when_multiple_exist() -> None:
    with _session_with_tables(include_reference=True) as session:
        _seed_instrument(session)
        _seed_bridge_facts(session, period_end=date(2025, 3, 31), revenue="1200", starting_id=1)
        _seed_bridge_facts(session, period_end=date(2026, 3, 31), revenue="1500", starting_id=100)

        projection = FundamentalsBridge(session).build_projection_input(
            instrument_id=101,
            as_of_date=date(2026, 7, 28),
            forecast_years=2,
            wacc=Decimal("0.11"),
            revenue_cagr=Decimal("0.08"),
            ebit_margin=Decimal("0.22"),
        )

        assert projection is not None
        assert projection.revenue_base == Decimal("1500")


def test_api_integration_marks_avis_fundamentals_source() -> None:
    with _session_with_tables(include_reference=True, include_valuation=True) as session:
        instrument_uuid = _seed_instrument(session)
        _seed_bridge_facts(session, period_end=date(2026, 3, 31), revenue="1500")
        _seed_relative_target_metrics(session)
        session.commit()

        request = RunValuationRequest(
            instrument_uuid=instrument_uuid,
            run_label="fundamentals-integration",
            assumption_set=AssumptionSetRequest(
                wacc=Decimal("0.11"),
                terminal_growth=Decimal("0.04"),
                forecast_years=3,
                revenue_cagr=Decimal("0.08"),
                ebit_margin=Decimal("0.22"),
                da=None,
                capex=None,
                nwc_delta=None,
            ),
        )

        queued = queue_valuation_run_sync(session, request)
        process_queued_valuation_run_sync(session, queued.val_run_id)

        dcf_output = session.scalar(
            select(ValModelOutput).where(
                ValModelOutput.val_run_id == queued.val_run_id,
                ValModelOutput.model_name == "DCF",
            )
        )
        assert dcf_output is not None
        assert dcf_output.output_payload["fundamentals_source"] == "AVIS"


def _session_with_tables(*, include_ops: bool = False, include_reference: bool = False, include_valuation: bool = False):
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def _enable_sqlite_features(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        dbapi_connection.create_function("GREATEST", 2, max)
        dbapi_connection.create_function("LEAST", 2, min)

    tables = [FundStatementFact.__table__, FundMetricFact.__table__]
    if include_reference:
        tables.extend([RefExchange.__table__, RefCompany.__table__, RefInstrument.__table__])
    if include_ops:
        tables.extend([OpsPipelineRun.__table__, OpsJobEvent.__table__, OpsDataLineage.__table__])
    if include_valuation:
        tables.extend(
            [
                DqRule.__table__,
                DqResult.__table__,
                DqIncident.__table__,
                OpsPipelineRun.__table__,
                ValRun.__table__,
                ValAssumptionSet.__table__,
                ValModelOutput.__table__,
                ValAttribution.__table__,
                ValConfidenceSnapshot.__table__,
            ]
        )
    Base.metadata.create_all(engine, tables=tables)
    return Session(engine)


def _seed_instrument(session: Session) -> uuid.UUID:
    now = datetime(2026, 7, 28, 9, 0)
    instrument_uuid = uuid.uuid4()
    session.add(
        RefExchange(
            exchange_id=1,
            exchange_code="NSE",
            exchange_name="National Stock Exchange",
            country_code="IN",
            timezone="Asia/Kolkata",
            currency_code="INR",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
    )
    session.add(
        RefCompany(
            company_id=10,
            company_uuid=uuid.uuid4(),
            legal_name="ITC Limited",
            display_name="ITC",
            isin_primary="INE154A01025",
            sector_code="FMCG",
            industry_code="FMCG_CORE",
            incorporation_country="IN",
            is_listed=True,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
    )
    session.add(
        RefInstrument(
            instrument_id=101,
            instrument_uuid=instrument_uuid,
            company_id=10,
            exchange_id=1,
            symbol="ITC",
            instrument_type="EQUITY",
            listing_date=date(2020, 1, 1),
            delisting_date=None,
            tick_size=Decimal("0.05"),
            lot_size=1,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
    )
    session.flush()
    return instrument_uuid


def _statement_fact(statement_fact_id: int, instrument_id: int, line_item_code: str, value: str, period_end: date) -> FundStatementFact:
    ts_value = datetime.combine(period_end, datetime.min.time())
    return FundStatementFact(
        statement_fact_id=statement_fact_id,
        instrument_id=instrument_id,
        fiscal_year=period_end.year,
        fiscal_period="FY",
        statement_type="PL" if line_item_code in {TOTAL_REVENUE, "GROSS_PROFIT", "NET_INCOME"} else "BS",
        line_item_code=line_item_code,
        value=Decimal(value),
        currency_code="INR",
        scale_code="ABS",
        as_reported_ts=ts_value,
        effective_from_ts=ts_value,
        effective_to_ts=None,
        source_system="TEST",
        source_record_id=f"{line_item_code}-{period_end.isoformat()}",
        ingested_at=datetime(2026, 7, 28, 9, 0),
    )


def _seed_bridge_facts(session: Session, *, period_end: date, revenue: str, starting_id: int = 1) -> None:
    ts_value = datetime.combine(period_end, datetime.min.time())
    rows = [
        FundStatementFact(
            statement_fact_id=starting_id,
            instrument_id=101,
            fiscal_year=period_end.year,
            fiscal_period="FY",
            statement_type="PL",
            line_item_code=TOTAL_REVENUE,
            value=Decimal(revenue),
            currency_code="INR",
            scale_code="ABS",
            as_reported_ts=ts_value,
            effective_from_ts=ts_value,
            effective_to_ts=None,
            source_system="YFINANCE",
            source_record_id=f"rev-{period_end.isoformat()}",
            ingested_at=datetime(2026, 7, 28, 9, 0),
        ),
        FundStatementFact(
            statement_fact_id=starting_id + 1,
            instrument_id=101,
            fiscal_year=period_end.year,
            fiscal_period="FY",
            statement_type="CF",
            line_item_code=DEPRECIATION,
            value=Decimal("60"),
            currency_code="INR",
            scale_code="ABS",
            as_reported_ts=ts_value,
            effective_from_ts=ts_value,
            effective_to_ts=None,
            source_system="YFINANCE",
            source_record_id=f"dep-{period_end.isoformat()}",
            ingested_at=datetime(2026, 7, 28, 9, 0),
        ),
        FundStatementFact(
            statement_fact_id=starting_id + 2,
            instrument_id=101,
            fiscal_year=period_end.year,
            fiscal_period="FY",
            statement_type="CF",
            line_item_code=CAPEX,
            value=Decimal("80"),
            currency_code="INR",
            scale_code="ABS",
            as_reported_ts=ts_value,
            effective_from_ts=ts_value,
            effective_to_ts=None,
            source_system="YFINANCE",
            source_record_id=f"capex-{period_end.isoformat()}",
            ingested_at=datetime(2026, 7, 28, 9, 0),
        ),
        FundStatementFact(
            statement_fact_id=starting_id + 3,
            instrument_id=101,
            fiscal_year=period_end.year,
            fiscal_period="FY",
            statement_type="BS",
            line_item_code=TOTAL_DEBT,
            value=Decimal("500"),
            currency_code="INR",
            scale_code="ABS",
            as_reported_ts=ts_value,
            effective_from_ts=ts_value,
            effective_to_ts=None,
            source_system="YFINANCE",
            source_record_id=f"debt-{period_end.isoformat()}",
            ingested_at=datetime(2026, 7, 28, 9, 0),
        ),
        FundStatementFact(
            statement_fact_id=starting_id + 4,
            instrument_id=101,
            fiscal_year=period_end.year,
            fiscal_period="FY",
            statement_type="BS",
            line_item_code=CASH,
            value=Decimal("150"),
            currency_code="INR",
            scale_code="ABS",
            as_reported_ts=ts_value,
            effective_from_ts=ts_value,
            effective_to_ts=None,
            source_system="YFINANCE",
            source_record_id=f"cash-{period_end.isoformat()}",
            ingested_at=datetime(2026, 7, 28, 9, 0),
        ),
        FundStatementFact(
            statement_fact_id=starting_id + 5,
            instrument_id=101,
            fiscal_year=period_end.year,
            fiscal_period="FY",
            statement_type="BS",
            line_item_code=SHARES_OUTSTANDING,
            value=Decimal("100"),
            currency_code="INR",
            scale_code="ABS",
            as_reported_ts=ts_value,
            effective_from_ts=ts_value,
            effective_to_ts=None,
            source_system="YFINANCE",
            source_record_id=f"shares-{period_end.isoformat()}",
            ingested_at=datetime(2026, 7, 28, 9, 0),
        ),
    ]
    session.add_all(rows)
    session.flush()


def _seed_relative_target_metrics(session: Session) -> None:
    now = datetime(2026, 7, 28, 9, 0)
    metric_values = {
        "ENTERPRISE_VALUE": "1500",
        "EQUITY_VALUE": "1300",
        "EBITDA": "160",
        "EARNINGS": "110",
        "BOOK_VALUE": "700",
        "SALES": "1200",
        "NET_DEBT": "350",
        "SHARE_COUNT": "100",
    }
    metric_id = 1
    for metric_code, metric_value in metric_values.items():
        session.add(
            FundMetricFact(
                metric_fact_id=metric_id,
                instrument_id=101,
                metric_code=metric_code,
                metric_value=Decimal(metric_value),
                metric_unit="amount",
                as_of_date=date(2026, 3, 31),
                lookback_period=None,
                calc_version="v1",
                input_hash=f"hash-{metric_code}",
                created_at=now,
            )
        )
        metric_id += 1
    session.flush()


def _fake_ticker() -> FakeTicker:
    period = pd.Timestamp("2026-03-31")
    income_stmt = pd.DataFrame(
        {
            period: {
                "Total Revenue": 1500,
                "Gross Profit": 600,
                "Normalized EBITDA": 300,
                "Operating Income": 240,
                "Net Income": 180,
                "Interest Expense": 25,
                "Tax Provision": 55,
            }
        }
    )
    balance_sheet = pd.DataFrame(
        {
            period: {
                "Total Assets": 3200,
                "Total Liabilities Net Minority Interest": 1300,
                "Stockholders Equity": 1900,
                "Total Debt": 500,
                "Net Debt": 350,
                "Cash And Cash Equivalents": 150,
                "Accounts Receivable": 210,
                "Inventory": 110,
                "Accounts Payable": 95,
            }
        }
    )
    cashflow = pd.DataFrame(
        {
            period: {
                "Operating Cash Flow": 260,
                "Capital Expenditure": -80,
                "Free Cash Flow": 180,
                "Depreciation And Amortization": 60,
            }
        }
    )
    return FakeTicker(
        income_stmt=income_stmt,
        balance_sheet=balance_sheet,
        cashflow=cashflow,
        info={"marketCap": 10000, "currentPrice": 100, "currentRatio": 1.5, "quickRatio": 1.1},
    )
