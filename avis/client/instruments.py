"""Instrument client wrapper for AVIS API."""

from __future__ import annotations

from datetime import date
import pandas as pd
import requests

from avis.client.base import AVISClient
from avis.client.models import InstrumentDetail, InstrumentResult, InstrumentListResponse, PriceHistory, PriceHistoryRow


class InstrumentsClient(AVISClient):
    def search(self, query: str) -> list[InstrumentResult] | None:
        if not self.enabled:
            return None
        normalized = query.strip().casefold()
        if not normalized:
            return []

        items: list[InstrumentResult] = []
        page = 1
        total = None
        while total is None or len(items) < total:
            payload = self._get("/api/v1/instruments", params={"page": page, "page_size": 200})
            parsed = self._parse_dataclass(payload, InstrumentListResponse)
            filtered = [
                item for item in parsed.items
                if normalized in item.symbol.casefold()
                or normalized in item.company_name.casefold()
                or (item.isin and normalized in item.isin.casefold())
            ]
            items.extend(filtered)
            total = parsed.pagination.total
            if (page * parsed.pagination.page_size) >= parsed.pagination.total:
                break
            page += 1
        return items or self._yahoo_search(query)

    def get(self, instrument_uuid: str) -> InstrumentDetail | None:
        payload = self._get(f"/api/v1/instruments/{instrument_uuid}")
        if payload is None:
            return None
        return self._parse_dataclass(payload, InstrumentDetail)

    def price_history(
        self,
        instrument_uuid: str,
        from_date: str | date,
        to_date: str | date,
        adjusted: bool = True,
    ) -> pd.DataFrame | None:
        payload = self._get(
            f"/api/v1/instruments/{instrument_uuid}/price-history",
            params={
                "from_date": _date_value(from_date),
                "to_date": _date_value(to_date),
                "adjusted": str(adjusted).lower(),
            },
        )
        if payload is None:
            return None
        parsed = self._parse_dataclass(payload, PriceHistory)
        return _price_history_dataframe(parsed.rows)

    def _yahoo_search(self, query: str) -> list[InstrumentResult]:
        yahoo_session = requests.Session()
        try:
            response = yahoo_session.get(
                "https://query2.finance.yahoo.com/v1/finance/search",
                params={"q": query, "quotesCount": 10, "newsCount": 0},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        finally:
            close = getattr(yahoo_session, "close", None)
            if callable(close):
                close()
        results: list[InstrumentResult] = []
        for item in payload.get("quotes", []):
            symbol = item.get("symbol")
            if not symbol:
                continue
            results.append(
                InstrumentResult(
                    instrument_uuid=None,
                    symbol=symbol,
                    isin=item.get("isin"),
                    exchange=item.get("exchange") or item.get("exchDisp"),
                    company_name=item.get("shortname") or item.get("longname") or symbol,
                )
            )
        return results


def _price_history_dataframe(rows: list[PriceHistoryRow]) -> pd.DataFrame:
    frame = pd.DataFrame(
        [
            {
                "date": pd.to_datetime(row.trade_date),
                "open": row.open_px,
                "high": row.high_px,
                "low": row.low_px,
                "close": row.adjusted_close_px if row.adjusted_close_px is not None else row.close_px,
                "volume": row.volume,
            }
            for row in rows
        ],
        columns=["date", "open", "high", "low", "close", "volume"],
    )
    return frame


def _date_value(value: str | date) -> str:
    return value.isoformat() if isinstance(value, date) else value
