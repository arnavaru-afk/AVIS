from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from avis.core.entity_resolution import (
    AliasConflictError,
    AliasRegistryService,
    CanonicalIdentityResolver,
    CorporateActionAdjustmentService,
    EntityNotFoundError,
)
from avis.core.entity_resolution.alias_registry import AliasMutation
from avis.db.base import Base
from avis.db.models import (
    MarketOhlcv1D,
    RefCompany,
    RefExchange,
    RefInstrument,
)


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, _connection_record) -> None:
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
            Base.metadata.tables["market_corporate_action"],
            Base.metadata.tables["ref_symbol_alias"],
        ],
    )
    with Session(engine) as db_session:
        yield db_session


def test_canonical_identity_resolver_acceptance_criteria(session: Session) -> None:
    company_uuid = _seed_itc(session)
    resolver = CanonicalIdentityResolver(session)

    assert resolver.resolve("ITC") == company_uuid
    assert resolver.resolve("ITC.NS") == company_uuid
    assert resolver.resolve("INE154A01025") == company_uuid

    detailed = resolver.resolve_with_metadata("ITC.NS")
    assert detailed.company_uuid == company_uuid
    assert detailed.instrument_id == 101
    assert detailed.rule_used == "TICKER_EXACT"
    assert detailed.source_provenance == "ref_instrument.symbol+ref_exchange.exchange_code=NSE"
    assert detailed.confidence_score >= 90


def test_alias_registry_rejects_overlapping_conflicts(session: Session) -> None:
    _seed_exchange(session, 1, "NSE")
    company_one = _seed_company(
        session,
        company_id=1,
        company_uuid=uuid.uuid4(),
        display_name="Alpha",
        isin="INE000A01001",
    )
    company_two = _seed_company(
        session,
        company_id=2,
        company_uuid=uuid.uuid4(),
        display_name="Beta",
        isin="INE000B01002",
    )
    _seed_instrument(session, 11, company_one.company_id, 1, "ALPHA")
    _seed_instrument(session, 22, company_two.company_id, 1, "BETA")

    registry = AliasRegistryService(session)
    registry.create_alias(
        AliasMutation(
            instrument_id=11,
            source_system="NSE",
            alias_symbol="ALPHA.NS",
            valid_from=date(2023, 1, 1),
        )
    )

    with pytest.raises(AliasConflictError):
        registry.create_alias(
            AliasMutation(
                instrument_id=22,
                source_system="NSE",
                alias_symbol="ALPHA.NS",
                valid_from=date(2024, 1, 1),
            )
        )


def test_point_in_time_lookup_respects_alias_windows(session: Session) -> None:
    _seed_itc(session)
    registry = AliasRegistryService(session)
    registry.create_alias(
        AliasMutation(
            instrument_id=101,
            source_system="BLOOMBERG",
            alias_symbol="ITCC IN",
            valid_from=date(2024, 1, 1),
            valid_to=date(2024, 12, 31),
        )
    )
    registry.create_alias(
        AliasMutation(
            instrument_id=101,
            source_system="REFINITIV",
            alias_symbol="ITC.N",
            valid_from=date(2025, 1, 1),
        )
    )

    resolver = CanonicalIdentityResolver(session)

    assert resolver.resolve_as_of("ITCC IN", "NSE", date(2024, 6, 1)) == 101
    with pytest.raises(EntityNotFoundError):
        resolver.resolve_as_of("ITC.N", "NSE", date(2024, 6, 1))
    with pytest.raises(EntityNotFoundError):
        resolver.resolve_as_of("ITCC IN", "NSE", date(2025, 1, 2))


def test_fuzzy_alias_resolution_uses_levenshtein_threshold(session: Session) -> None:
    company_uuid = _seed_itc(session)
    resolver = CanonicalIdentityResolver(session)

    detailed = resolver.resolve_with_metadata("ITC.NX")

    assert detailed.company_uuid == company_uuid
    assert detailed.rule_used == "ALIAS_FUZZY"
    assert detailed.matched_input == "ITC.NS"
    assert detailed.confidence_score < 90


def test_full_round_trip_resolve_to_adjusted_price(session: Session) -> None:
    company_uuid = _seed_itc(session)
    _seed_ohlcv(session, 101, date(2024, 1, 1), "200")
    _seed_ohlcv(session, 101, date(2024, 2, 29), "120")

    adjustments = CorporateActionAdjustmentService(session)
    adjustments.create_action(
        instrument_id=101,
        action_type="split",
        ex_date=date(2024, 2, 1),
        action_ratio_num=Decimal("2"),
        action_ratio_den=Decimal("1"),
        source_system="NSE",
    )
    adjustments.create_action(
        instrument_id=101,
        action_type="dividend",
        ex_date=date(2024, 3, 1),
        action_value=Decimal("5"),
        source_system="NSE",
    )

    resolver = CanonicalIdentityResolver(session)
    assert resolver.resolve("ITC.NS") == company_uuid
    adjusted = adjustments.adjusted_close(101, date(2024, 1, 1))

    assert adjusted == Decimal("95.833333")


def test_delisted_instrument_is_not_returned_post_delisting(session: Session) -> None:
    _seed_exchange(session, 1, "NSE")
    company = _seed_company(
        session,
        company_id=3,
        company_uuid=uuid.uuid4(),
        display_name="Delisted Co",
        isin="INE999D01010",
    )
    _seed_instrument(
        session,
        instrument_id=301,
        company_id=company.company_id,
        exchange_id=1,
        symbol="DLST",
        delisting_date=date(2024, 1, 31),
    )

    resolver = CanonicalIdentityResolver(session)
    assert resolver.resolve_as_of("DLST", "NSE", date(2024, 1, 15)) == 301
    with pytest.raises(EntityNotFoundError):
        resolver.resolve_as_of("DLST", "NSE", date(2024, 2, 1))


def test_dual_listed_instrument_requires_exchange_disambiguation(session: Session) -> None:
    company_uuid = uuid.uuid4()
    company = _seed_company(
        session,
        company_id=4,
        company_uuid=company_uuid,
        display_name="Dual Listed",
        isin="INE888D01010",
    )
    _seed_exchange(session, 1, "NSE")
    _seed_exchange(session, 2, "BSE")
    _seed_instrument(session, 401, company.company_id, 1, "DUAL")
    _seed_instrument(session, 402, company.company_id, 2, "DUAL")

    resolver = CanonicalIdentityResolver(session)

    assert resolver.resolve("DUAL.NS") == company_uuid
    assert resolver.resolve("DUAL", exchange_mic="BSE") == company_uuid
    assert resolver.resolve_as_of("DUAL", "NSE", date(2024, 5, 1)) == 401
    assert resolver.resolve_as_of("DUAL", "BSE", date(2024, 5, 1)) == 402


def test_post_merger_symbol_lookup_is_point_in_time_correct(session: Session) -> None:
    _seed_exchange(session, 1, "NSE")
    company = _seed_company(
        session,
        company_id=5,
        company_uuid=uuid.uuid4(),
        display_name="Merged Co",
        isin="INE777M01010",
    )
    _seed_instrument(session, 501, company.company_id, 1, "NEWCO")

    registry = AliasRegistryService(session)
    registry.create_alias(
        AliasMutation(
            instrument_id=501,
            source_system="NSE",
            alias_symbol="OLDCO",
            valid_from=date(2023, 1, 1),
            valid_to=date(2024, 3, 31),
        )
    )
    registry.create_alias(
        AliasMutation(
            instrument_id=501,
            source_system="NSE",
            alias_symbol="NEWCO",
            valid_from=date(2024, 4, 1),
        )
    )

    actions = CorporateActionAdjustmentService(session)
    actions.create_action(
        instrument_id=501,
        action_type="merger",
        ex_date=date(2024, 4, 1),
        source_system="NSE",
    )

    resolver = CanonicalIdentityResolver(session)
    assert resolver.resolve_as_of("OLDCO", "NSE", date(2024, 3, 15)) == 501
    assert resolver.resolve_as_of("NEWCO", "NSE", date(2024, 4, 15)) == 501
    with pytest.raises(EntityNotFoundError):
        resolver.resolve_as_of("OLDCO", "NSE", date(2024, 4, 15))


def test_alias_transition_boundary_is_exact_at_cutover(session: Session) -> None:
    _seed_exchange(session, 1, "NSE")
    company = _seed_company(
        session,
        company_id=6,
        company_uuid=uuid.uuid4(),
        display_name="Boundary Co",
        isin="INE666B01010",
    )
    _seed_instrument(session, 601, company.company_id, 1, "NEWBN")

    registry = AliasRegistryService(session)
    registry.create_alias(
        AliasMutation(
            instrument_id=601,
            source_system="NSE",
            alias_symbol="OLDBN",
            valid_from=date(2024, 1, 1),
            valid_to=date(2024, 3, 31),
        )
    )
    registry.create_alias(
        AliasMutation(
            instrument_id=601,
            source_system="NSE",
            alias_symbol="NEWBN",
            valid_from=date(2024, 4, 1),
        )
    )

    resolver = CanonicalIdentityResolver(session)
    assert resolver.resolve_as_of("OLDBN", "NSE", date(2024, 3, 31)) == 601
    with pytest.raises(EntityNotFoundError):
        resolver.resolve_as_of("OLDBN", "NSE", date(2024, 4, 1))
    assert resolver.resolve_as_of("NEWBN", "NSE", date(2024, 4, 1)) == 601


def _seed_itc(session: Session) -> uuid.UUID:
    _seed_exchange(session, 1, "NSE")
    company_uuid = uuid.uuid4()
    company = _seed_company(
        session,
        company_id=10,
        company_uuid=company_uuid,
        display_name="ITC",
        isin="INE154A01025",
    )
    _seed_instrument(session, 101, company.company_id, 1, "ITC")

    registry = AliasRegistryService(session)
    registry.create_alias(
        AliasMutation(
            instrument_id=101,
            source_system="ISIN",
            alias_symbol="INE154A01025",
            valid_from=date(2000, 1, 1),
        )
    )
    registry.create_alias(
        AliasMutation(
            instrument_id=101,
            source_system="NSE",
            alias_symbol="ITC.NS",
            valid_from=date(2000, 1, 1),
        )
    )
    session.commit()
    return company_uuid


def _seed_exchange(session: Session, exchange_id: int, exchange_code: str) -> None:
    if session.get(RefExchange, exchange_id) is not None:
        return
    session.add(
        RefExchange(
            exchange_id=exchange_id,
            exchange_code=exchange_code,
            exchange_name=exchange_code,
            country_code="IN",
            timezone="Asia/Kolkata",
            currency_code="INR",
            is_active=True,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    session.flush()


def _seed_company(
    session: Session,
    *,
    company_id: int,
    company_uuid: uuid.UUID,
    display_name: str,
    isin: str,
) -> RefCompany:
    company = RefCompany(
        company_id=company_id,
        company_uuid=company_uuid,
        legal_name=display_name,
        display_name=display_name,
        isin_primary=isin,
        sector_code=None,
        industry_code=None,
        incorporation_country="IN",
        is_listed=True,
        is_active=True,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(company)
    session.flush()
    return company


def _seed_instrument(
    session: Session,
    instrument_id: int,
    company_id: int,
    exchange_id: int,
    symbol: str,
    *,
    delisting_date: date | None = None,
) -> RefInstrument:
    instrument = RefInstrument(
        instrument_id=instrument_id,
        instrument_uuid=uuid.uuid4(),
        company_id=company_id,
        exchange_id=exchange_id,
        symbol=symbol,
        instrument_type="EQUITY",
        listing_date=date(2000, 1, 1),
        delisting_date=delisting_date,
        tick_size=Decimal("0.05"),
        lot_size=1,
        is_active=delisting_date is None,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(instrument)
    session.flush()
    return instrument


def _seed_ohlcv(session: Session, instrument_id: int, trade_date: date, close_px: str) -> None:
    session.add(
        MarketOhlcv1D(
            ohlcv_1d_id=int(f"{instrument_id}{trade_date.strftime('%m%d')}"),
            instrument_id=instrument_id,
            trade_date=trade_date,
            open_px=Decimal(close_px),
            high_px=Decimal(close_px),
            low_px=Decimal(close_px),
            close_px=Decimal(close_px),
            adj_close_px=None,
            volume=Decimal("1000"),
            turnover=Decimal(close_px) * Decimal("1000"),
            source_system="NSE",
            source_record_id=f"{instrument_id}:{trade_date.isoformat()}",
            ingested_at=_now(),
        )
    )
    session.flush()


def _now() -> datetime:
    return datetime.now(timezone.utc)
