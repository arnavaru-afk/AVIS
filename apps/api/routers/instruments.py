"""Reference instrument API router."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Query, Request
from sqlalchemy import Select, func, select
from sqlalchemy.orm import selectinload

from apps.api.dependencies import (
    CurrentUser,
    DateWindow,
    DbSession,
    PaginationParams,
    register_compliance_check,
)
from apps.api.routers.market import _fetch_price_rows, _load_instrument
from apps.api.schemas.instruments import (
    InstrumentDetailResponse,
    InstrumentListItem,
    InstrumentListResponse,
    PaginationMeta,
    PriceHistoryResponse,
    SymbolAliasResponse,
)
from avis.db.models import RefCompany, RefExchange, RefInstrument

router = APIRouter(prefix="/instruments", tags=["instruments"])


@router.get("", response_model=InstrumentListResponse)
async def list_instruments(
    db: DbSession,
    _current_user: CurrentUser,
    pagination: PaginationParams,
    exchange: str | None = Query(default=None),
    sector: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
) -> InstrumentListResponse:
    stmt = (
        select(RefInstrument, RefCompany, RefExchange)
        .join(RefCompany, RefInstrument.company_id == RefCompany.company_id)
        .join(RefExchange, RefInstrument.exchange_id == RefExchange.exchange_id)
        .order_by(RefInstrument.instrument_id.asc())
    )
    count_stmt = (
        select(func.count())
        .select_from(RefInstrument)
        .join(RefCompany, RefInstrument.company_id == RefCompany.company_id)
        .join(RefExchange, RefInstrument.exchange_id == RefExchange.exchange_id)
    )
    if exchange:
        stmt = stmt.where(RefExchange.exchange_code == exchange.upper())
        count_stmt = count_stmt.where(RefExchange.exchange_code == exchange.upper())
    if sector:
        stmt = stmt.where(RefCompany.sector_code == sector)
        count_stmt = count_stmt.where(RefCompany.sector_code == sector)
    if is_active is not None:
        stmt = stmt.where(RefInstrument.is_active == is_active)
        count_stmt = count_stmt.where(RefInstrument.is_active == is_active)

    total = int((await db.scalar(count_stmt)) or 0)
    rows = (await db.execute(stmt.offset(pagination.offset).limit(pagination.page_size))).all()
    items = [
        InstrumentListItem(
            instrument_uuid=instrument.instrument_uuid,
            symbol=instrument.symbol,
            isin=company.isin_primary,
            exchange=exchange_row.exchange_code,
            company_name=company.display_name,
        )
        for instrument, company, exchange_row in rows
    ]
    return InstrumentListResponse(
        items=items,
        pagination=PaginationMeta(page=pagination.page, page_size=pagination.page_size, total=total),
    )


@router.get("/{instrument_uuid}", response_model=InstrumentDetailResponse)
async def get_instrument(
    instrument_uuid: UUID,
    db: DbSession,
    _current_user: CurrentUser,
) -> InstrumentDetailResponse:
    stmt: Select[tuple[RefInstrument]] = (
        select(RefInstrument)
        .options(
            selectinload(RefInstrument.company),
            selectinload(RefInstrument.exchange),
            selectinload(RefInstrument.symbol_aliases),
        )
        .where(RefInstrument.instrument_uuid == instrument_uuid)
    )
    instrument = await db.scalar(stmt)
    if instrument is None:
        raise _not_found()

    today = date.today()
    aliases = [
        SymbolAliasResponse.model_validate(alias)
        for alias in instrument.symbol_aliases
        if alias.valid_to is None or alias.valid_to >= today
    ]
    return InstrumentDetailResponse(
        instrument_uuid=instrument.instrument_uuid,
        symbol=instrument.symbol,
        isin=instrument.company.isin_primary,
        exchange=instrument.exchange.exchange_code,
        company_name=instrument.company.display_name,
        is_active=instrument.is_active,
        sector=instrument.company.sector_code,
        aliases=aliases,
    )


@router.get("/{instrument_uuid}/price-history", response_model=PriceHistoryResponse)
async def get_instrument_price_history(
    instrument_uuid: UUID,
    request: Request,
    db: DbSession,
    _current_user: CurrentUser,
    date_window: DateWindow,
    adjusted: bool = Query(default=True),
    as_of_date: date | None = Query(default=None),
) -> PriceHistoryResponse:
    instrument = await _load_instrument(db, instrument_uuid)
    from_date, to_date = date_window
    rows = await _fetch_price_rows(
        db,
        instrument,
        from_date=from_date,
        to_date=to_date,
        adjusted=adjusted,
        as_of_date=as_of_date,
    )
    for source_system in {row.source_system for row in rows}:
        register_compliance_check(
            request,
            source_name=source_system,
            resource_id=f"market_ohlcv_1d:{instrument.instrument_id}",
            attribution=f"Source: {source_system}",
        )
    return PriceHistoryResponse(
        instrument_uuid=instrument.instrument_uuid,
        adjusted=adjusted,
        rows=rows,
    )


def _not_found():
    from fastapi import HTTPException, status

    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "instrument_not_found", "message": "Unknown instrument_uuid"},
    )
