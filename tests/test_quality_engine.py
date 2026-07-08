from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from avis.core.quality_engine import (
    CompliancePolicyEngine,
    CompliancePolicyError,
    QualityGateEngine,
    SourcePolicy,
)
from avis.db.base import Base
from avis.db.models import (
    AuthAuditEvent,
    AuthUser,
    DqIncident,
    DqOverride,
    DqResult,
    DqRule,
    MarketOhlcv1D,
    OpsDataLineage,
    OpsJobEvent,
    OpsPipelineRun,
    RefCompany,
    RefExchange,
    RefInstrument,
)


@dataclass(slots=True)
class QualityRecord:
    instrument_id: int | None
    trade_date: date
    source_system: str
    open_px: Decimal
    high_px: Decimal
    low_px: Decimal
    close_px: Decimal
    event_ts: datetime
    symbol: str = "ITC"


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
            MarketOhlcv1D.__table__,
            OpsPipelineRun.__table__,
            OpsJobEvent.__table__,
            OpsDataLineage.__table__,
            DqRule.__table__,
            DqResult.__table__,
            DqIncident.__table__,
            AuthUser.__table__,
            DqOverride.__table__,
            AuthAuditEvent.__table__,
        ],
    )
    with Session(engine) as db_session:
        _seed_reference_state(db_session)
        yield db_session


def test_null_check_in_isolation(session: Session, tmp_path: Path) -> None:
    gate = QualityGateEngine(
        session=session,
        quarantine_root=tmp_path / "quarantine",
        now_provider=_fixed_now,
    )
    pipeline_run = _seed_pipeline_run(session, "dq_null_check")

    decision = gate.evaluate_record(
        QualityRecord(
            instrument_id=101,
            trade_date=date(2026, 6, 26),
            source_system="NSE_EOD",
            open_px=Decimal("100"),
            high_px=Decimal("101"),
            low_px=Decimal("99"),
            close_px=Decimal("100"),
            event_ts=datetime(2026, 6, 26, 10, 0, tzinfo=timezone.utc),
        ),
        pipeline_run_id=pipeline_run.pipeline_run_id,
        source_table="normalize.market_ohlcv",
        target_table="market_ohlcv_1d",
        non_nullable_fields=("instrument_id", "trade_date", "source_system", "event_ts", "turnover"),
    )

    assert decision.allowed is False
    assert "NULL_CHECK_V1" in decision.blocking_rule_codes


def test_range_check_in_isolation(session: Session, tmp_path: Path) -> None:
    gate = QualityGateEngine(
        session=session,
        quarantine_root=tmp_path / "quarantine",
        now_provider=_fixed_now,
    )
    pipeline_run = _seed_pipeline_run(session, "dq_range_check")

    decision = gate.evaluate_record(
        QualityRecord(
            instrument_id=101,
            trade_date=date(2026, 6, 26),
            source_system="NSE_EOD",
            open_px=Decimal("0.001"),
            high_px=Decimal("101"),
            low_px=Decimal("99"),
            close_px=Decimal("100"),
            event_ts=datetime(2026, 6, 26, 10, 0, tzinfo=timezone.utc),
        ),
        pipeline_run_id=pipeline_run.pipeline_run_id,
        source_table="normalize.market_ohlcv",
        target_table="market_ohlcv_1d",
        non_nullable_fields=("instrument_id", "trade_date", "source_system", "event_ts"),
    )

    assert decision.allowed is False
    assert decision.quarantined is True


def test_stale_check_in_isolation(session: Session, tmp_path: Path) -> None:
    gate = QualityGateEngine(
        session=session,
        quarantine_root=tmp_path / "quarantine",
        now_provider=lambda: datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc),
    )
    pipeline_run = _seed_pipeline_run(session, "dq_stale_check")

    decision = gate.evaluate_record(
        QualityRecord(
            instrument_id=101,
            trade_date=date(2026, 6, 26),
            source_system="NSE_EOD",
            open_px=Decimal("100"),
            high_px=Decimal("101"),
            low_px=Decimal("99"),
            close_px=Decimal("100"),
            event_ts=datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc),
        ),
        pipeline_run_id=pipeline_run.pipeline_run_id,
        source_table="normalize.market_ohlcv",
        target_table="market_ohlcv_1d",
        non_nullable_fields=("instrument_id", "trade_date", "source_system", "event_ts"),
    )

    assert decision.allowed is False
    assert "STALE_CHECK_V1" in decision.blocking_rule_codes


def test_duplicate_check_in_isolation(session: Session, tmp_path: Path) -> None:
    _seed_market_row(session, instrument_id=101, trade_date=date(2026, 6, 26), close_px="100", source_system="NSE_EOD")
    gate = QualityGateEngine(
        session=session,
        quarantine_root=tmp_path / "quarantine",
        now_provider=_fixed_now,
    )
    pipeline_run = _seed_pipeline_run(session, "dq_duplicate_check")

    decision = gate.evaluate_record(
        QualityRecord(
            instrument_id=101,
            trade_date=date(2026, 6, 26),
            source_system="NSE_EOD",
            open_px=Decimal("100"),
            high_px=Decimal("101"),
            low_px=Decimal("99"),
            close_px=Decimal("100"),
            event_ts=datetime(2026, 6, 26, 10, 0, tzinfo=timezone.utc),
        ),
        pipeline_run_id=pipeline_run.pipeline_run_id,
        source_table="normalize.market_ohlcv",
        target_table="market_ohlcv_1d",
        non_nullable_fields=("instrument_id", "trade_date", "source_system", "event_ts"),
    )

    assert decision.allowed is False
    assert "DUPLICATE_CHECK_V1" in decision.blocking_rule_codes


def test_symbol_resolved_check_in_isolation(session: Session, tmp_path: Path) -> None:
    gate = QualityGateEngine(
        session=session,
        quarantine_root=tmp_path / "quarantine",
        now_provider=_fixed_now,
    )
    pipeline_run = _seed_pipeline_run(session, "dq_symbol_check")

    decision = gate.evaluate_record(
        QualityRecord(
            instrument_id=None,
            trade_date=date(2026, 6, 26),
            source_system="NSE_EOD",
            open_px=Decimal("100"),
            high_px=Decimal("101"),
            low_px=Decimal("99"),
            close_px=Decimal("100"),
            event_ts=datetime(2026, 6, 26, 10, 0, tzinfo=timezone.utc),
        ),
        pipeline_run_id=pipeline_run.pipeline_run_id,
        source_table="normalize.market_ohlcv",
        target_table="market_ohlcv_1d",
        non_nullable_fields=("trade_date", "source_system", "event_ts"),
    )

    assert decision.allowed is False
    assert "SYMBOL_RESOLVED_CHECK_V1" in decision.blocking_rule_codes


def test_circuit_breaker_in_isolation(session: Session, tmp_path: Path) -> None:
    _seed_market_row(session, instrument_id=101, trade_date=date(2026, 6, 25), close_px="100", source_system="NSE_EOD")
    gate = QualityGateEngine(
        session=session,
        quarantine_root=tmp_path / "quarantine",
        now_provider=_fixed_now,
    )
    pipeline_run = _seed_pipeline_run(session, "dq_circuit_breaker")

    inserted: list[QualityRecord] = []
    decision = gate.evaluate_record(
        QualityRecord(
            instrument_id=101,
            trade_date=date(2026, 6, 26),
            source_system="NSE_EOD",
            open_px=Decimal("100"),
            high_px=Decimal("170"),
            low_px=Decimal("99"),
            close_px=Decimal("170"),
            event_ts=datetime(2026, 6, 26, 10, 0, tzinfo=timezone.utc),
        ),
        pipeline_run_id=pipeline_run.pipeline_run_id,
        source_table="normalize.market_ohlcv",
        target_table="market_ohlcv_1d",
        non_nullable_fields=("instrument_id", "trade_date", "source_system", "event_ts"),
        persist_fn=inserted.append,
    )

    assert decision.allowed is False
    assert decision.quarantined is False
    assert inserted == []
    assert session.scalar(
        select(OpsJobEvent).where(OpsJobEvent.event_type == "ERROR")
    ) is not None


def test_quarantine_routing_and_override_recording(session: Session, tmp_path: Path) -> None:
    gate = QualityGateEngine(
        session=session,
        quarantine_root=tmp_path / "quarantine",
        now_provider=_fixed_now,
    )
    pipeline_run = _seed_pipeline_run(session, "dq_override_flow")

    decision = gate.evaluate_record(
        QualityRecord(
            instrument_id=101,
            trade_date=date(2026, 6, 26),
            source_system="NSE_EOD",
            open_px=Decimal("0.001"),
            high_px=Decimal("101"),
            low_px=Decimal("99"),
            close_px=Decimal("100"),
            event_ts=datetime(2026, 6, 26, 10, 0, tzinfo=timezone.utc),
        ),
        pipeline_run_id=pipeline_run.pipeline_run_id,
        source_table="normalize.market_ohlcv",
        target_table="market_ohlcv_1d",
        non_nullable_fields=("instrument_id", "trade_date", "source_system", "event_ts"),
    )

    assert decision.quarantined is True
    assert decision.quarantined_path is not None
    incident = session.scalar(select(DqIncident).order_by(DqIncident.dq_incident_id.desc()))
    assert incident is not None

    override = gate.record_override(
        incident_id=incident.dq_incident_id,
        approved_by_user_id=9001,
        justification="Approved because source correction is pending.",
    )

    assert override.dq_incident_id == incident.dq_incident_id
    assert session.scalar(select(DqOverride.dq_override_id)) is not None


def test_critical_row_never_reaches_curated_store(session: Session, tmp_path: Path) -> None:
    _seed_market_row(session, instrument_id=101, trade_date=date(2026, 6, 25), close_px="100", source_system="NSE_EOD")
    gate = QualityGateEngine(
        session=session,
        quarantine_root=tmp_path / "quarantine",
        now_provider=_fixed_now,
    )
    pipeline_run = _seed_pipeline_run(session, "dq_integration_gate")

    def persist(record: QualityRecord) -> None:
        _seed_market_row(
            session,
            instrument_id=record.instrument_id or 0,
            trade_date=record.trade_date,
            close_px=str(record.close_px),
            source_system=record.source_system,
        )

    batch = gate.process_records(
        [
            QualityRecord(
                instrument_id=101,
                trade_date=date(2026, 6, 26),
                source_system="NSE_EOD",
                open_px=Decimal("100"),
                high_px=Decimal("170"),
                low_px=Decimal("99"),
                close_px=Decimal("170"),
                event_ts=datetime(2026, 6, 26, 10, 0, tzinfo=timezone.utc),
            )
        ],
        pipeline_run_id=pipeline_run.pipeline_run_id,
        source_table="normalize.market_ohlcv",
        target_table="market_ohlcv_1d",
        transform_id="quality_gate.market_ohlcv_1d",
        non_nullable_fields=("instrument_id", "trade_date", "source_system", "event_ts"),
        persist_fn=persist,
    )

    assert batch.row_count_out == 0
    assert session.scalar(
        select(func.count()).select_from(MarketOhlcv1D).where(MarketOhlcv1D.trade_date == date(2026, 6, 26))
    ) == 0


def test_compliance_restricted_source_blocked_at_api_boundary(session: Session) -> None:
    engine = CompliancePolicyEngine(session=session)
    engine.register_policy(
        SourcePolicy(
            source_name="NSE_EOD",
            redistribution_allowed=False,
            commercial_use=True,
            attribution_required=True,
        )
    )

    with pytest.raises(CompliancePolicyError):
        engine.evaluate_api_access(
            source_name="NSE_EOD",
            external_call=True,
            derived_output=True,
            attribution="Source: NSE_EOD",
            user_id=9001,
            resource_id="market_ohlcv_1d",
        )

    audit_event = session.scalar(
        select(AuthAuditEvent).where(AuthAuditEvent.event_type == "COMPLIANCE_POLICY_BLOCK")
    )
    assert audit_event is not None


def test_lineage_emitted_for_pipeline_run(session: Session, tmp_path: Path) -> None:
    gate = QualityGateEngine(
        session=session,
        quarantine_root=tmp_path / "quarantine",
        now_provider=_fixed_now,
    )
    pipeline_run = _seed_pipeline_run(session, "dq_lineage")

    persisted: list[QualityRecord] = []
    batch = gate.process_records(
        [
            QualityRecord(
                instrument_id=101,
                trade_date=date(2026, 6, 26),
                source_system="NSE_EOD",
                open_px=Decimal("100"),
                high_px=Decimal("101"),
                low_px=Decimal("99"),
                close_px=Decimal("100"),
                event_ts=datetime(2026, 6, 26, 10, 0, tzinfo=timezone.utc),
            ),
            QualityRecord(
                instrument_id=101,
                trade_date=date(2026, 6, 27),
                source_system="NSE_EOD",
                open_px=Decimal("0.001"),
                high_px=Decimal("101"),
                low_px=Decimal("99"),
                close_px=Decimal("100"),
                event_ts=datetime(2026, 6, 27, 10, 0, tzinfo=timezone.utc),
            ),
        ],
        pipeline_run_id=pipeline_run.pipeline_run_id,
        source_table="normalize.market_ohlcv",
        target_table="market_ohlcv_1d",
        transform_id="quality_gate.market_ohlcv_1d",
        non_nullable_fields=("instrument_id", "trade_date", "source_system", "event_ts"),
        persist_fn=persisted.append,
    )

    lineage = session.scalar(select(OpsDataLineage))
    assert lineage is not None
    assert lineage.record_count_in == 2
    assert lineage.record_count_out == 1
    assert batch.failed_count == 1


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
    session.add(
        AuthUser(
            user_id=9001,
            user_uuid=uuid.uuid4(),
            email="analyst@avis.test",
            full_name="AVIS Analyst",
            status="ACTIVE",
            is_mfa_enabled=True,
            last_login_at=None,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    session.commit()


def _seed_pipeline_run(session: Session, pipeline_name: str) -> OpsPipelineRun:
    run = OpsPipelineRun(
        pipeline_run_id=_next_id(session, OpsPipelineRun, "pipeline_run_id"),
        run_uuid=uuid.uuid4(),
        pipeline_name=pipeline_name,
        run_mode="TEST",
        triggered_by="PYTEST",
        status="RUNNING",
        started_at=_now(),
        ended_at=None,
        run_context=None,
    )
    session.add(run)
    session.flush()
    return run


def _seed_market_row(
    session: Session,
    *,
    instrument_id: int,
    trade_date: date,
    close_px: str,
    source_system: str,
) -> None:
    session.add(
        MarketOhlcv1D(
            ohlcv_1d_id=_next_id(session, MarketOhlcv1D, "ohlcv_1d_id"),
            instrument_id=instrument_id,
            trade_date=trade_date,
            open_px=Decimal(close_px),
            high_px=Decimal(close_px),
            low_px=Decimal(close_px),
            close_px=Decimal(close_px),
            adj_close_px=Decimal(close_px),
            volume=Decimal("1000"),
            turnover=Decimal(close_px) * Decimal("1000"),
            source_system=source_system,
            source_record_id=f"{instrument_id}:{trade_date.isoformat()}",
            ingested_at=_now(),
        )
    )
    session.flush()


def _next_id(session: Session, model, column_name: str) -> int:
    column = getattr(model, column_name)
    current_max = session.scalar(select(func.max(column)))
    return 1 if current_max is None else int(current_max) + 1


def _fixed_now() -> datetime:
    return datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
