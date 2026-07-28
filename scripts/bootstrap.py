"""Idempotent development bootstrap for AVIS reference and auth data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import os
from uuid import UUID, uuid5, NAMESPACE_DNS

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from avis.db.models import AuthRole, AuthUser, AuthUserRole, RefCompany, RefExchange, RefInstrument, RefSymbolAlias
from scripts.dev_token import DEV_ANALYST_EMAIL, DEV_ANALYST_UUID

CANONICAL_LISTING_DATE = date(2000, 1, 1)
NOW = lambda: datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class CompanySeed:
    company_id: int
    instrument_id: int
    symbol: str
    legal_name: str
    display_name: str
    isin: str
    bse_code: str
    sector_code: str
    industry_code: str


COMPANIES = [
    CompanySeed(1001, 2001, "ITC", "ITC Limited", "ITC", "INE154A01025", "500875", "FMCG", "FMCG_CORE"),
    CompanySeed(1002, 2002, "RELIANCE", "Reliance Industries Limited", "RELIANCE", "INE002A01018", "500325", "ENERGY", "ENERGY_INTEGRATED"),
    CompanySeed(1003, 2003, "TCS", "Tata Consultancy Services Limited", "TCS", "INE467B01029", "532540", "IT", "IT_SERVICES"),
    CompanySeed(1004, 2004, "HDFCBANK", "HDFC Bank Limited", "HDFCBANK", "INE040A01034", "500180", "BANK", "BANK_PRIVATE"),
    CompanySeed(1005, 2005, "INFY", "Infosys Limited", "INFY", "INE009A01021", "500209", "IT", "IT_SERVICES"),
]

ROLE_SEEDS = [
    {"role_id": 1, "role_code": "ANALYST", "role_name": "Analyst"},
    {"role_id": 2, "role_code": "ADMIN", "role_name": "Administrator"},
]


def get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")
    if database_url is None or not database_url.strip():
        raise RuntimeError("DATABASE_URL is required")
    return database_url.strip()


def create_session_factory(database_url: str):
    engine = create_engine(database_url, future=True)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def seed_database(session: Session) -> dict[str, int]:
    _seed_exchanges(session)
    _seed_companies_and_instruments(session)
    _seed_auth(session)
    session.commit()
    return {
        "exchanges": _count_rows(session, RefExchange),
        "companies": _count_rows(session, RefCompany),
        "instruments": _count_rows(session, RefInstrument),
        "aliases": _count_rows(session, RefSymbolAlias),
    }


def _seed_exchanges(session: Session) -> None:
    now = NOW()
    _ensure_exchange(
        session,
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
    _ensure_exchange(
        session,
        exchange_id=2,
        exchange_code="BSE",
        exchange_name="Bombay Stock Exchange",
        country_code="IN",
        timezone="Asia/Kolkata",
        currency_code="INR",
        is_active=True,
        created_at=now,
        updated_at=now,
    )


def _seed_companies_and_instruments(session: Session) -> None:
    now = NOW()
    for seed in COMPANIES:
        company = session.scalar(select(RefCompany).where(RefCompany.company_id == seed.company_id))
        if company is None:
            company = session.scalar(select(RefCompany).where(RefCompany.isin_primary == seed.isin))
        if company is None:
            company = RefCompany(
                company_id=seed.company_id,
                company_uuid=uuid5(NAMESPACE_DNS, f"avis-company-{seed.symbol}"),
                legal_name=seed.legal_name,
                display_name=seed.display_name,
                isin_primary=seed.isin,
                sector_code=seed.sector_code,
                industry_code=seed.industry_code,
                incorporation_country="IN",
                is_listed=True,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            session.add(company)
        else:
            company.legal_name = seed.legal_name
            company.display_name = seed.display_name
            company.isin_primary = seed.isin
            company.sector_code = seed.sector_code
            company.industry_code = seed.industry_code
            company.incorporation_country = "IN"
            company.is_listed = True
            company.is_active = True
            company.updated_at = now
        session.flush()

        instrument = session.scalar(select(RefInstrument).where(RefInstrument.instrument_id == seed.instrument_id))
        if instrument is None:
            instrument = session.scalar(
                select(RefInstrument).where(
                    RefInstrument.company_id == company.company_id,
                    RefInstrument.symbol == seed.symbol,
                )
            )
        if instrument is None:
            instrument = RefInstrument(
                instrument_id=seed.instrument_id,
                instrument_uuid=uuid5(NAMESPACE_DNS, f"avis-instrument-{seed.symbol}"),
                company_id=company.company_id,
                exchange_id=1,
                symbol=seed.symbol,
                instrument_type="EQUITY",
                listing_date=CANONICAL_LISTING_DATE,
                delisting_date=None,
                tick_size=Decimal("0.05"),
                lot_size=1,
                is_active=True,
                created_at=now,
                updated_at=now,
            )
            session.add(instrument)
        else:
            instrument.company_id = company.company_id
            instrument.exchange_id = 1
            instrument.symbol = seed.symbol
            instrument.instrument_type = "EQUITY"
            instrument.listing_date = CANONICAL_LISTING_DATE
            instrument.delisting_date = None
            instrument.tick_size = Decimal("0.05")
            instrument.lot_size = 1
            instrument.is_active = True
            instrument.updated_at = now
        session.flush()

        alias_values = [
            ("NSE", seed.symbol),
            ("BSE", seed.bse_code),
            ("ISIN", seed.isin),
        ]
        for source_system, alias_symbol in alias_values:
            _ensure_alias(
                session,
                instrument_id=instrument.instrument_id,
                source_system=source_system,
                alias_symbol=alias_symbol,
                valid_from=CANONICAL_LISTING_DATE,
                valid_to=None,
                created_at=now,
            )


def _seed_auth(session: Session) -> None:
    now = NOW()
    for role_seed in ROLE_SEEDS:
        role = session.scalar(select(AuthRole).where(AuthRole.role_code == role_seed["role_code"]))
        if role is None:
            role = AuthRole(
                role_id=role_seed["role_id"],
                role_code=role_seed["role_code"],
                role_name=role_seed["role_name"],
                description=None,
                is_system_role=True,
                created_at=now,
            )
            session.add(role)
        else:
            role.role_name = role_seed["role_name"]
            role.is_system_role = True
        session.flush()

    user = session.scalar(select(AuthUser).where(AuthUser.user_uuid == DEV_ANALYST_UUID))
    if user is None:
        user = session.scalar(select(AuthUser).where(AuthUser.email == DEV_ANALYST_EMAIL))
    if user is None:
        user = AuthUser(
            user_id=1,
            user_uuid=DEV_ANALYST_UUID,
            email=DEV_ANALYST_EMAIL,
            full_name="AVIS Dev Analyst",
            status="ACTIVE",
            is_mfa_enabled=False,
            last_login_at=None,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
    else:
        user.user_uuid = DEV_ANALYST_UUID
        user.email = DEV_ANALYST_EMAIL
        user.full_name = "AVIS Dev Analyst"
        user.status = "ACTIVE"
        user.is_mfa_enabled = False
        user.updated_at = now
    session.flush()

    valid_from = now - timedelta(days=1)
    for role_code in ("ANALYST", "ADMIN"):
        role = session.scalar(select(AuthRole).where(AuthRole.role_code == role_code))
        existing = session.scalar(
            select(AuthUserRole).where(
                AuthUserRole.user_id == user.user_id,
                AuthUserRole.role_id == role.role_id,
                AuthUserRole.valid_to.is_(None),
            )
        )
        if existing is None:
            next_id = _next_bigint_id(session, AuthUserRole, "user_role_id")
            session.add(
                AuthUserRole(
                    user_role_id=next_id,
                    user_id=user.user_id,
                    role_id=role.role_id,
                    granted_by_user_id=None,
                    valid_from=valid_from,
                    valid_to=None,
                    created_at=now,
                )
            )


def _ensure_exchange(session: Session, **payload) -> None:
    exchange = session.scalar(select(RefExchange).where(RefExchange.exchange_code == payload["exchange_code"]))
    if exchange is None:
        session.add(RefExchange(**payload))
        return
    for key, value in payload.items():
        setattr(exchange, key, value)


def _ensure_alias(session: Session, **payload) -> None:
    alias = session.scalar(
        select(RefSymbolAlias).where(
            RefSymbolAlias.instrument_id == payload["instrument_id"],
            RefSymbolAlias.source_system == payload["source_system"],
            RefSymbolAlias.alias_symbol == payload["alias_symbol"],
            RefSymbolAlias.valid_from == payload["valid_from"],
            RefSymbolAlias.valid_to.is_(payload["valid_to"]),
        )
    )
    if alias is None:
        session.add(
            RefSymbolAlias(
                alias_id=_next_bigint_id(session, RefSymbolAlias, "alias_id"),
                **payload,
            )
        )
        return
    alias.valid_to = payload["valid_to"]
    alias.created_at = payload["created_at"]


def _next_bigint_id(session: Session, model, pk_name: str) -> int:
    current_max = session.scalar(select(func.max(getattr(model, pk_name))))
    return 1 if current_max is None else int(current_max) + 1


def _count_rows(session: Session, model) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def main() -> None:
    session_factory = create_session_factory(get_database_url())
    with session_factory() as session:
        counts = seed_database(session)
    print(
        f"Seeded {counts['companies']} companies, {counts['instruments']} instruments, {counts['aliases']} aliases across {counts['exchanges']} exchanges."
    )


if __name__ == "__main__":
    main()
