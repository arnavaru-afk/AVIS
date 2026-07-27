"""Market client wrapper for AVIS API."""

from __future__ import annotations

from datetime import date

import pandas as pd

from avis.client.base import AVISClient
from avis.client.models import PriceHistory, PriceHistoryRow


class MarketClient(AVISClient):
    def ohlcv(
        self,
        instrument_uuid: str,
        from_date: str | date,
        to_date: str | date,
        adjusted: bool = True,
    ) -> pd.DataFrame | None:
        payload = self._get(
            f"/api/v1/market/{instrument_uuid}/ohlcv",
            params={
                "from_date": _date_value(from_date),
                "to_date": _date_value(to_date),
                "adjusted": str(adjusted).lower(),
            },
        )
        if payload is None:
            return None
        parsed = self._parse_dataclass(payload, PriceHistory)
        return pd.DataFrame(
            [
                {
                    "date": pd.to_datetime(row.trade_date),
                    "open": row.open_px,
                    "high": row.high_px,
                    "low": row.low_px,
                    "close": row.adjusted_close_px if row.adjusted_close_px is not None else row.close_px,
                    "volume": row.volume,
                }
                for row in parsed.rows
            ],
            columns=["date", "open", "high", "low", "close", "volume"],
        )


def _date_value(value: str | date) -> str:
    return value.isoformat() if isinstance(value, date) else value
