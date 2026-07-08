from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from apps.api.schemas import AvisSchema


class PaginationMeta(AvisSchema):
    page: int
    page_size: int
    total: int


class SymbolAliasResponse(AvisSchema):
    alias_id: int
    source_system: str
    alias_symbol: str
    valid_from: date
    valid_to: date | None


class InstrumentListItem(AvisSchema):
    instrument_uuid: UUID
    symbol: str
    isin: str | None
    exchange: str
    company_name: str


class InstrumentListResponse(AvisSchema):
    items: list[InstrumentListItem]
    pagination: PaginationMeta


class InstrumentDetailResponse(InstrumentListItem):
    is_active: bool
    sector: str | None
    aliases: list[SymbolAliasResponse]


class PriceHistoryRow(AvisSchema):
    trade_date: date
    open_px: Decimal
    high_px: Decimal
    low_px: Decimal
    close_px: Decimal
    adjusted_close_px: Decimal | None
    volume: Decimal
    source_system: str


class PriceHistoryResponse(AvisSchema):
    instrument_uuid: UUID
    adjusted: bool
    rows: list[PriceHistoryRow]
