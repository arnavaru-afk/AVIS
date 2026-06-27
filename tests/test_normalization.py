from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from avis.core.entity_resolution import AliasRegistryService, CanonicalIdentityResolver
from avis.core.entity_resolution.alias_registry import AliasMutation
from avis.db.base import Base
from avis.db.models import (
    DqIncident,
    DqResult,
    DqRule,
    MarketCorporateAction,
    MarketOhlcv1D,
    OpsJobEvent,
    OpsPipelineRun,
    RefCompany,
    RefExchange,
    RefInstrument,
    RefSymbolAlias,
)
from etl.ingest.connectors.nse_eod import NseEodConnector
from etl.normalize import OhlcvNormalizationService


class StaticFxRateProvider:
    def __init__(self, rates: dict[tuple[str, str, date], Decimal]) -> None:
        self._rates = rates

    def get_rate(self, from_currency: str, to_currency: str, as_of_date: date) -> Decimal | None:
        return self._rates.get((from_currency, to_currency, as_of_date))


def test_symbol_resolution_and_curated_insert(tmp_path: Path) -> None:
    with _session() as session:
        _seed_market(session)
        resolver = CanonicalIdentityResolver(session)
        service = OhlcvNormalizationService(session=session, resolver=resolver)

        record = service.normalize_row(
            {
                "symbol": "ITC",
                "timestamp": "2026-06-26",
                "open": "100",
                "high": "102",
                "low": "99",
                "close": "101",
                "volume": "1000",
            },
            exchange="NSE",
            source_system="NSE_EOD",
            currency_code="INR",
            ingested_ts=datetime(2026, 6, 26, 13, 30, tzinfo=timezone.utc),
            source_record_id="row-1",
        )

        assert record is not None
        row = service.upsert_record(record)
        assert row.instrument_id == 101
        assert row.close_px == Decimal("101")


def test_missing_fx_rate_creates_dq_incident() -> None:
    with _session() as session:
        _seed_market(session)
        resolver = CanonicalIdentityResolver(session)
        service = OhlcvNormalizationService(
            session=session,
            resolver=resolver,
            fx_rate_provider=StaticFxRateProvider({}),
        )

        record = service.normalize_row(
            {
                "symbol": "ITC",
                "timestamp": "2026-06-26",
                "open": "1",
                "high": "2",
                "low": "1",
                "close": "1.5",
                "volume": "100",
            },
            exchange="NSE",
            source_system="NSE_EOD",
            currency_code="USD",
            ingested_ts=datetime(2026, 6, 26, 13, 30, tzinfo=timezone.utc),
            source_record_id="row-fx",
        )

        assert record is None
        assert session.scalar(select(DqIncident.dq_incident_id)) is not None


def test_future_timestamp_guard_quarantines_row() -> None:
    with _session() as session:
        _seed_market(session)
        resolver = CanonicalIdentityResolver(session)
        service = OhlcvNormalizationService(
            session=session,
            resolver=resolver,
            now_provider=lambda: datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc),
        )

        record = service.normalize_row(
            {
                "symbol": "ITC",
                "timestamp": "2026-06-26",
                "open": "100",
                "high": "102",
                "low": "99",
                "close": "101",
                "volume": "1000",
            },
            exchange="NSE",
            source_system="NSE_EOD",
            currency_code="INR",
            ingested_ts=datetime(2026, 6, 26, 13, 30, tzinfo=timezone.utc),
            source_record_id="row-future",
            event_ts=datetime(2026, 6, 26, 14, 30, tzinfo=timezone.utc),
        )

        assert record is None
        result = session.scalar(select(DqResult).where(DqResult.failure_reason == "future_event_ts"))
        assert result is not None


def test_circuit_breaker_rejects_adjusted_close_outlier() -> None:
    with _session() as session:
        _seed_market(session)
        _seed_previous_close(session, instrument_id=101, trade_date=date(2026, 6, 25), close_px="100")
        _seed_split(session, instrument_id=101, ex_date=date(2026, 6, 27), ratio_num="10", ratio_den="1")
        resolver = CanonicalIdentityResolver(session)
        service = OhlcvNormalizationService(session=session, resolver=resolver)

        record = service.normalize_row(
            {
                "symbol": "ITC",
                "timestamp": "2026-06-26",
                "open": "100",
                "high": "102",
                "low": "99",
                "close": "101",
                "volume": "1000",
            },
            exchange="NSE",
            source_system="NSE_EOD",
            currency_code="INR",
            ingested_ts=datetime(2026, 6, 26, 13, 30, tzinfo=timezone.utc),
            source_record_id="row-breaker",
        )

        assert record is None
        assert session.scalar(
            select(DqResult).where(DqResult.failure_reason == "adjusted_close_outlier")
        ) is not None


def test_duplicate_row_keeps_latest_ingested_version() -> None:
    with _session() as session:
        _seed_market(session)
        resolver = CanonicalIdentityResolver(session)
        service = OhlcvNormalizationService(session=session, resolver=resolver)

        first = service.normalize_row(
            {
                "symbol": "ITC",
                "timestamp": "2026-06-26",
                "open": "100",
                "high": "102",
                "low": "99",
                "close": "101",
                "volume": "1000",
            },
            exchange="NSE",
            source_system="NSE_EOD",
            currency_code="INR",
            ingested_ts=datetime(2026, 6, 26, 13, 30, tzinfo=timezone.utc),
            source_record_id="dup-1",
        )
        second = service.normalize_row(
            {
                "symbol": "ITC",
                "timestamp": "2026-06-26",
                "open": "110",
                "high": "112",
                "low": "109",
                "close": "111",
                "volume": "1200",
            },
            exchange="NSE",
            source_system="NSE_EOD",
            currency_code="INR",
            ingested_ts=datetime(2026, 6, 26, 14, 30, tzinfo=timezone.utc),
            source_record_id="dup-2",
        )

        assert first is not None
        assert second is not None
        service.upsert_record(first)
        updated = service.upsert_record(second)

        assert updated.close_px == Decimal("111")
        assert updated.source_record_id == "dup-2"
        assert session.scalar(
            select(DqResult).where(DqResult.failure_reason == "superseded_row")
        ) is not None


def test_raw_to_normalize_to_curated_round_trip_for_five_instruments(tmp_path: Path) -> None:
    with _session() as session:
        _seed_market(session, include_five=True)
        connector = NseEodConnector(
            session=session,
            raw_zone_root=tmp_path / "raw",
            contract_path=Path("data/contracts/nse_eod.json"),
            fetcher=lambda _url: _five_instrument_payload(),
        )
        connector.run(date(2026, 6, 26))

        resolver = CanonicalIdentityResolver(session)
        service = OhlcvNormalizationService(session=session, resolver=resolver)
        manifest_path = tmp_path / "raw" / "NSE_EOD" / "2026-06-26" / "manifest.json"
        normalized = service.normalize_manifest(
            manifest_path,
            exchange="NSE",
            source_system="NSE_EOD",
        )

        assert len(normalized) == 5
        assert session.scalar(select(func.count()).select_from(MarketOhlcv1D)) == 5


def _session():
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
            RefSymbolAlias.__table__,
            MarketOhlcv1D.__table__,
            MarketCorporateAction.__table__,
            DqRule.__table__,
            DqResult.__table__,
            DqIncident.__table__,
            OpsPipelineRun.__table__,
            OpsJobEvent.__table__,
        ],
    )
    return Session(engine)


def _seed_market(session: Session, *, include_five: bool = False) -> None:
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
    symbols = [("ITC", 10, 101), ("INFY", 20, 201), ("TCS", 30, 301), ("HDFCBANK", 40, 401), ("RELIANCE", 50, 501)]
    if not include_five:
        symbols = symbols[:1]
    registry = AliasRegistryService(session)
    for symbol, company_id, instrument_id in symbols:
        session.add(
            RefCompany(
                company_id=company_id,
                company_uuid=uuid.uuid4(),
                legal_name=symbol,
                display_name=symbol,
                isin_primary=f"INE{company_id:03d}A01025",
                sector_code=None,
                industry_code=None,
                incorporation_country="IN",
                is_listed=True,
                is_active=True,
                created_at=_now(),
                updated_at=_now(),
            )
        )
        session.flush()
        session.add(
            RefInstrument(
                instrument_id=instrument_id,
                instrument_uuid=uuid.uuid4(),
                company_id=company_id,
                exchange_id=1,
                symbol=symbol,
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
        session.flush()
        registry.create_alias(
            AliasMutation(
                instrument_id=instrument_id,
                source_system="NSE",
                alias_symbol=symbol,
                valid_from=date(2000, 1, 1),
            )
        )
    session.commit()


def _seed_previous_close(session: Session, *, instrument_id: int, trade_date: date, close_px: str) -> None:
    session.add(
        MarketOhlcv1D(
            ohlcv_1d_id=instrument_id * 1000 + trade_date.day,
            instrument_id=instrument_id,
            trade_date=trade_date,
            open_px=Decimal(close_px),
            high_px=Decimal(close_px),
            low_px=Decimal(close_px),
            close_px=Decimal(close_px),
            adj_close_px=Decimal(close_px),
            volume=Decimal("1000"),
            turnover=Decimal(close_px) * Decimal("1000"),
            source_system="NSE_EOD",
            source_record_id=f"seed:{trade_date.isoformat()}",
            ingested_at=_now(),
        )
    )
    session.flush()


def _seed_split(
    session: Session,
    *,
    instrument_id: int,
    ex_date: date,
    ratio_num: str,
    ratio_den: str,
) -> None:
    session.add(
        MarketCorporateAction(
            corp_action_id=instrument_id * 100 + ex_date.day,
            instrument_id=instrument_id,
            action_type="SPLIT",
            announcement_date=ex_date,
            ex_date=ex_date,
            record_date=ex_date,
            effective_date=ex_date,
            action_value=None,
            action_ratio_num=Decimal(ratio_num),
            action_ratio_den=Decimal(ratio_den),
            currency_code=None,
            source_system="NSE",
            source_record_id=f"split:{ex_date.isoformat()}",
            ingested_at=_now(),
        )
    )
    session.flush()


def _five_instrument_payload() -> bytes:
    return (
        "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,TOTTRDQTY,DELIV_QTY,TIMESTAMP\n"
        "ITC,EQ,100,102,99,101,1000,450,2026-06-26\n"
        "INFY,EQ,1500,1510,1490,1502,900,300,2026-06-26\n"
        "TCS,EQ,3400,3415,3380,3402,700,250,2026-06-26\n"
        "HDFCBANK,EQ,1680,1692,1675,1688,950,310,2026-06-26\n"
        "RELIANCE,EQ,2900,2912,2888,2905,1100,500,2026-06-26\n"
    ).encode("utf-8")


def _now() -> datetime:
    return datetime.now(timezone.utc)
