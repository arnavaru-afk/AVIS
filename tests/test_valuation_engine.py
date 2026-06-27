from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from avis.core.valuation_engine import (
    AssumptionSetManager,
    ConfidenceScoringEngine,
    DcfAssumptions,
    DcfEngine,
    DcfProjectionInput,
    ImmutableAssumptionSetError,
    InsufficientPeerSetError,
    RelativeEngine,
    RelativeInputBundle,
    RelativePeerPoint,
    ValuationOrchestrator,
)
from avis.db.base import Base
from avis.db.models import (
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


class StaticCompPeerMap:
    def __init__(self, mapping: dict[int, list[int]]) -> None:
        self._mapping = mapping

    def get_peer_ids(self, instrument_id: int, *, as_of_date: date) -> list[int]:
        return list(self._mapping.get(instrument_id, []))


class StaticPeerMetricsProvider:
    def __init__(self, metrics: dict[int, RelativePeerPoint]) -> None:
        self._metrics = metrics

    def get_metrics(self, instrument_id: int, *, as_of_date: date) -> RelativePeerPoint:
        return self._metrics[instrument_id]


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def _enable_sqlite_features(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        dbapi_connection.create_function("GREATEST", 2, max)
        dbapi_connection.create_function("LEAST", 2, min)

    Base.metadata.create_all(
        engine,
        tables=[
            RefExchange.__table__,
            RefCompany.__table__,
            RefInstrument.__table__,
            OpsPipelineRun.__table__,
            ValRun.__table__,
            ValAssumptionSet.__table__,
            ValModelOutput.__table__,
            ValAttribution.__table__,
            ValConfidenceSnapshot.__table__,
        ],
    )
    with Session(engine) as db_session:
        _seed_reference_state(db_session)
        yield db_session


def test_fcf_formula_unit() -> None:
    engine = DcfEngine()
    assumptions = DcfAssumptions(
        wacc=Decimal("0.10"),
        terminal_growth=Decimal("0.03"),
        forecast_years=1,
        revenue_cagr=Decimal("0.00"),
        ebit_margin=Decimal("0.20"),
    )
    projection = DcfProjectionInput(
        revenue_base=Decimal("1000"),
        tax_rate=Decimal("0.25"),
        share_count=Decimal("10"),
        net_debt=Decimal("0"),
        depreciation_and_amortization=(Decimal("20"),),
        capex=(Decimal("30"),),
        delta_nwc=(Decimal("10"),),
    )

    result = engine.run(assumptions=assumptions, projection_input=projection)

    assert result.projection_lines[0].free_cash_flow == Decimal("130.00")


def test_terminal_value_formula_unit() -> None:
    terminal = DcfEngine.terminal_value(
        final_year_fcf=Decimal("100"),
        wacc=Decimal("0.10"),
        terminal_growth=Decimal("0.03"),
    )
    assert terminal == Decimal("1471.428571428571428571428571")


def test_multiple_implied_value_unit() -> None:
    target = RelativePeerPoint(
        instrument_id=101,
        enterprise_value=Decimal("0"),
        equity_value=Decimal("0"),
        ebitda=Decimal("100"),
        earnings=Decimal("80"),
        book_value=Decimal("200"),
        sales=Decimal("500"),
        net_debt=Decimal("50"),
        share_count=Decimal("10"),
    )
    equity_value, target_price = RelativeEngine.implied_value(
        multiple_name="EV/EBITDA",
        multiple=Decimal("8"),
        target_metrics=target,
    )

    assert equity_value == Decimal("750")
    assert target_price == Decimal("75")


def test_known_public_company_reasonable_range(session: Session) -> None:
    orchestrator = _orchestrator(session)
    assumptions = _default_assumptions()
    dcf_input = _default_dcf_input()
    relative_inputs = _default_relative_inputs()

    result = orchestrator.run(
        instrument_id=101,
        as_of_ts=datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc),
        dcf_assumptions=assumptions,
        dcf_projection_input=dcf_input,
        relative_inputs=relative_inputs,
        data_quality_score=Decimal("85"),
        assumption_stability=Decimal("75"),
    )

    assert Decimal("10") <= result.blended_target_price <= Decimal("200")


def test_negative_fcf_years_edge_case() -> None:
    engine = DcfEngine()
    assumptions = DcfAssumptions(
        wacc=Decimal("0.12"),
        terminal_growth=Decimal("0.03"),
        forecast_years=2,
        revenue_cagr=Decimal("0.00"),
        ebit_margin=Decimal("0.05"),
    )
    projection = DcfProjectionInput(
        revenue_base=Decimal("1000"),
        tax_rate=Decimal("0.25"),
        share_count=Decimal("10"),
        net_debt=Decimal("100"),
        depreciation_and_amortization=(Decimal("10"), Decimal("10")),
        capex=(Decimal("100"), Decimal("100")),
        delta_nwc=(Decimal("20"), Decimal("20")),
    )

    result = engine.run(assumptions=assumptions, projection_input=projection)

    assert result.projection_lines[0].free_cash_flow < 0
    assert result.projection_lines[1].free_cash_flow < 0


def test_insufficient_peer_set_edge_case(session: Session) -> None:
    engine = RelativeEngine(
        comp_peer_map=StaticCompPeerMap({101: [201]}),
        peer_metrics_provider=StaticPeerMetricsProvider({201: _peer_metric(201, "600", "500", "50", "40", "200", "300", "100", "10")}),
    )

    with pytest.raises(InsufficientPeerSetError):
        engine.run(
            target_instrument_id=101,
            as_of_date=date(2026, 6, 28),
            target_metrics=_target_metrics(),
        )


def test_wacc_must_exceed_growth_edge_case() -> None:
    engine = DcfEngine()
    with pytest.raises(ValueError):
        engine.run(
            assumptions=DcfAssumptions(
                wacc=Decimal("0.03"),
                terminal_growth=Decimal("0.03"),
                forecast_years=1,
                revenue_cagr=Decimal("0.05"),
                ebit_margin=Decimal("0.20"),
            ),
            projection_input=_default_dcf_input(),
        )


def test_reproducibility_same_assumption_set(session: Session) -> None:
    orchestrator = _orchestrator(session)
    assumptions = _default_assumptions()
    dcf_input = _default_dcf_input()
    relative_inputs = _default_relative_inputs()

    run_one = orchestrator.run(
        instrument_id=101,
        as_of_ts=datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc),
        dcf_assumptions=assumptions,
        dcf_projection_input=dcf_input,
        relative_inputs=relative_inputs,
        data_quality_score=Decimal("85"),
        assumption_stability=Decimal("75"),
    )
    run_two = orchestrator.run(
        instrument_id=101,
        as_of_ts=datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc),
        dcf_assumptions=assumptions,
        dcf_projection_input=dcf_input,
        relative_inputs=relative_inputs,
        data_quality_score=Decimal("85"),
        assumption_stability=Decimal("75"),
    )

    assert run_one.blended_target_price == run_two.blended_target_price
    assert run_one.dcf_result.enterprise_value == run_two.dcf_result.enterprise_value
    assert run_one.relative_result.point_estimate_target_price == run_two.relative_result.point_estimate_target_price


def test_confidence_low_score_triggers_override_flag(session: Session) -> None:
    orchestrator = _orchestrator(session)

    result = orchestrator.run(
        instrument_id=101,
        as_of_ts=datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc),
        dcf_assumptions=_default_assumptions(),
        dcf_projection_input=_default_dcf_input(),
        relative_inputs=_low_peer_relative_inputs(),
        data_quality_score=Decimal("20"),
        assumption_stability=Decimal("20"),
    )

    assert result.analyst_override_required is True
    val_run = session.get(ValRun, result.val_run_id)
    assert val_run is not None
    assert val_run.status == "OVERRIDDEN"
    assert session.scalar(
        select(ValAttribution).where(ValAttribution.driver_key == "ANALYST_OVERRIDE_REQUIRED")
    ) is not None


def test_assumption_sets_are_immutable(session: Session) -> None:
    manager = AssumptionSetManager(session)
    val_run = _seed_val_run(session, instrument_id=101)
    rows = manager.store_dcf_assumptions(val_run_id=val_run.val_run_id, assumptions=_default_assumptions())

    with pytest.raises(ImmutableAssumptionSetError):
        manager.update_assumption(assumption_set_id=rows[0].assumption_set_id, numeric_value=Decimal("0.12"))


def _orchestrator(session: Session) -> ValuationOrchestrator:
    peer_map = StaticCompPeerMap({101: [201, 202, 203, 204]})
    metrics_provider = StaticPeerMetricsProvider(
        {
            201: _peer_metric(201, "1800", "1600", "180", "120", "900", "1500", "200", "100"),
            202: _peer_metric(202, "1650", "1450", "170", "115", "850", "1400", "200", "100"),
            203: _peer_metric(203, "1750", "1500", "175", "118", "880", "1450", "250", "100"),
            204: _peer_metric(204, "1900", "1700", "190", "125", "920", "1520", "200", "100"),
        }
    )
    return ValuationOrchestrator(
        session=session,
        dcf_engine=DcfEngine(),
        relative_engine=RelativeEngine(
            comp_peer_map=peer_map,
            peer_metrics_provider=metrics_provider,
        ),
        confidence_engine=ConfidenceScoringEngine(),
    )


def _default_assumptions() -> DcfAssumptions:
    return DcfAssumptions(
        wacc=Decimal("0.11"),
        terminal_growth=Decimal("0.04"),
        forecast_years=5,
        revenue_cagr=Decimal("0.08"),
        ebit_margin=Decimal("0.24"),
    )


def _default_dcf_input() -> DcfProjectionInput:
    return DcfProjectionInput(
        revenue_base=Decimal("1000"),
        tax_rate=Decimal("0.25"),
        share_count=Decimal("100"),
        net_debt=Decimal("200"),
        depreciation_and_amortization=(
            Decimal("40"),
            Decimal("42"),
            Decimal("44"),
            Decimal("46"),
            Decimal("48"),
        ),
        capex=(
            Decimal("55"),
            Decimal("58"),
            Decimal("60"),
            Decimal("62"),
            Decimal("65"),
        ),
        delta_nwc=(
            Decimal("10"),
            Decimal("11"),
            Decimal("12"),
            Decimal("13"),
            Decimal("14"),
        ),
    )


def _default_relative_inputs() -> RelativeInputBundle:
    return RelativeInputBundle(
        target_instrument_id=101,
        as_of_date=datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc),
        target_metrics=_target_metrics(),
    )


def _low_peer_relative_inputs() -> RelativeInputBundle:
    return RelativeInputBundle(
        target_instrument_id=101,
        as_of_date=datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc),
        target_metrics=RelativePeerPoint(
            instrument_id=101,
            enterprise_value=Decimal("1400"),
            equity_value=Decimal("1200"),
            ebitda=Decimal("0.5"),
            earnings=Decimal("0.5"),
            book_value=Decimal("500"),
            sales=Decimal("1100"),
            net_debt=Decimal("200"),
            share_count=Decimal("100"),
        ),
    )


def _target_metrics() -> RelativePeerPoint:
    return RelativePeerPoint(
        instrument_id=101,
        enterprise_value=Decimal("1500"),
        equity_value=Decimal("1300"),
        ebitda=Decimal("160"),
        earnings=Decimal("110"),
        book_value=Decimal("700"),
        sales=Decimal("1200"),
        net_debt=Decimal("200"),
        share_count=Decimal("100"),
    )


def _peer_metric(
    instrument_id: int,
    enterprise_value: str,
    equity_value: str,
    ebitda: str,
    earnings: str,
    book_value: str,
    sales: str,
    net_debt: str,
    share_count: str,
) -> RelativePeerPoint:
    return RelativePeerPoint(
        instrument_id=instrument_id,
        enterprise_value=Decimal(enterprise_value),
        equity_value=Decimal(equity_value),
        ebitda=Decimal(ebitda),
        earnings=Decimal(earnings),
        book_value=Decimal(book_value),
        sales=Decimal(sales),
        net_debt=Decimal(net_debt),
        share_count=Decimal(share_count),
    )


def _seed_reference_state(session: Session) -> None:
    session.add(
        RefExchange(
            exchange_id=1,
            exchange_code="NSE",
            exchange_name="NSE",
            country_code="IN",
            timezone="Asia/Kolkata",
            currency_code="INR",
            is_active=True,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    session.add(
        RefCompany(
            company_id=10,
            company_uuid=uuid.uuid4(),
            legal_name="ITC",
            display_name="ITC",
            isin_primary="INE154A01025",
            sector_code=None,
            industry_code=None,
            incorporation_country="IN",
            is_listed=True,
            is_active=True,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    session.add(
        RefInstrument(
            instrument_id=101,
            instrument_uuid=uuid.uuid4(),
            company_id=10,
            exchange_id=1,
            symbol="ITC",
            instrument_type="EQUITY",
            listing_date=date(2000, 1, 1),
            delisting_date=None,
            tick_size=Decimal("0.05"),
            lot_size=1,
            is_active=True,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    session.commit()


def _seed_val_run(session: Session, *, instrument_id: int) -> ValRun:
    run = ValRun(
        val_run_id=1,
        run_uuid=uuid.uuid4(),
        instrument_id=instrument_id,
        as_of_ts=_now(),
        run_type="SCHEDULED",
        trigger_type="MANUAL",
        status="RUNNING",
        pipeline_run_id=None,
        created_at=_now(),
        completed_at=None,
    )
    session.add(run)
    session.flush()
    return run


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
