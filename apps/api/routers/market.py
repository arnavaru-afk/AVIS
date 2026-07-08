"""Market data API router."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import Select, distinct, select
from sqlalchemy.orm import selectinload

from apps.api.dependencies import CurrentUser, DateWindow, DbSession, register_compliance_check
from apps.api.schemas.market import PriceHistoryResponse, PriceHistoryRow
from avis.db.models import MarketOhlcv1D, RefCompany, RefExchange, RefInstrument

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/{instrument_uuid}/ohlcv", response_model=PriceHistoryResponse)
async def get_market_ohlcv(
    instrument_uuid: UUID,
    request: Request,
    db: DbSession,
    _current_user: CurrentUser,
    date_window: DateWindow,
    adjusted: bool = Query(default=True),
) -> PriceHistoryResponse:
    from_date, to_date = date_window
    instrument = await _load_instrument(db, instrument_uuid)
    rows = await _fetch_price_rows(db, instrument, from_date=from_date, to_date=to_date, adjusted=adjusted)
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


async def _load_instrument(db, instrument_uuid: UUID) -> RefInstrument:
    stmt: Select[tuple[RefInstrument]] = (
        select(RefInstrument)
        .options(selectinload(RefInstrument.company), selectinload(RefInstrument.exchange))
        .where(RefInstrument.instrument_uuid == instrument_uuid)
    )
    instrument = await db.scalar(stmt)
    if instrument is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "instrument_not_found", "message": "Unknown instrument_uuid"},
        )
    return instrument


async def _fetch_price_rows(
    db,
    instrument: RefInstrument,
    *,
    from_date: date | None,
    to_date: date | None,
    adjusted: bool,
) -> list[PriceHistoryRow]:
    effective_from = from_date
    if instrument.listing_date is not None:
        effective_from = max(filter(None, [from_date, instrument.listing_date]), default=instrument.listing_date)

    effective_to = to_date or date.today()
    if instrument.delisting_date is not None:
        effective_to = min(effective_to, instrument.delisting_date)

    stmt = (
        select(MarketOhlcv1D)
        .where(
            MarketOhlcv1D.instrument_id == instrument.instrument_id,
            MarketOhlcv1D.trade_date <= effective_to,
        )
        .order_by(MarketOhlcv1D.trade_date.asc())
    )
    if effective_from is not None:
        stmt = stmt.where(MarketOhlcv1D.trade_date >= effective_from)

    raw_rows = (await db.scalars(stmt)).all()
    output: list[PriceHistoryRow] = []
    for row in raw_rows:
        factor = Decimal("1")
        adjusted_close = row.adj_close_px
        if adjusted:
            if adjusted_close is None and row.close_px:
                adjusted_close = row.close_px
            if adjusted_close is not None and row.close_px:
                factor = Decimal(adjusted_close) / Decimal(row.close_px)
        output.append(
            PriceHistoryRow(
                trade_date=row.trade_date,
                open_px=(Decimal(row.open_px) * factor if adjusted else Decimal(row.open_px)).quantize(Decimal("0.000001")),
                high_px=(Decimal(row.high_px) * factor if adjusted else Decimal(row.high_px)).quantize(Decimal("0.000001")),
                low_px=(Decimal(row.low_px) * factor if adjusted else Decimal(row.low_px)).quantize(Decimal("0.000001")),
                close_px=(Decimal(adjusted_close) if adjusted and adjusted_close is not None else Decimal(row.close_px)).quantize(Decimal("0.000001")),
                adjusted_close_px=(Decimal(adjusted_close).quantize(Decimal("0.000001")) if adjusted_close is not None else None),
                volume=Decimal(row.volume),
                source_system=row.source_system,
            )
        )
    return output


__all__ = ["router", "_fetch_price_rows", "_load_instrument"]
