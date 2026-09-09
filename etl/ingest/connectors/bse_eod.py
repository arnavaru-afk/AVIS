"""BSE Bhav Copy end-of-day connector."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Mapping
from urllib.request import urlopen

from etl.ingest.base import BaseConnector, RawWriteResult, RetryPolicy

BSE_SERIES_MAP = {
    "A": "EQ",
    "B": "BE",
    "T": "BT",
    "MT": "MT",
    "SM": "SM",
}


class BseEodConnector(BaseConnector):
    """Downloads, validates, and stores BSE daily Bhav Copy payloads."""

    source_system = "BSE_EOD"
    pipeline_name = "bse_eod_ingest"
    job_name = "bse_eod_connector"

    def __init__(
        self,
        *,
        session,
        raw_zone_root: Path,
        contract_path: Path,
        scrip_code_to_isin: Mapping[str, str],
        fetcher: Callable[[str], bytes] | None = None,
        retry_policy: RetryPolicy | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        super().__init__(
            session=session,
            raw_zone_root=raw_zone_root,
            contract_path=contract_path,
            retry_policy=retry_policy,
            sleep_fn=sleep_fn,
        )
        self._scrip_code_to_isin = {
            key.strip(): value.strip().upper()
            for key, value in scrip_code_to_isin.items()
        }
        self._fetcher = fetcher or self._default_fetcher

    def fetch(self, trade_date: date) -> bytes:
        return self._fetcher(self._build_url(trade_date))

    def validate(self, payload: bytes, trade_date: date) -> list[dict[str, object]]:
        rows = list(csv.DictReader(io.StringIO(self._decode_payload(payload))))
        validated: list[dict[str, object]] = []
        required_fields = {
            "SC_CODE",
            "SC_NAME",
            "SC_GROUP",
            "OPEN",
            "HIGH",
            "LOW",
            "CLOSE",
            "NO_OF_SHRS",
            "DELIV_QTY",
            "DT_TM",
        }
        if not rows:
            return []
        if not required_fields.issubset(rows[0].keys()):
            missing = sorted(required_fields - set(rows[0].keys()))
            raise ValueError(f"Missing required BSE fields: {missing}")

        for row in rows:
            scrip_code = self._require_text(row, "SC_CODE")
            if scrip_code not in self._scrip_code_to_isin:
                raise ValueError(f"Missing ISIN mapping for BSE scrip code {scrip_code}")
            open_px = self._require_non_negative_decimal(row, "OPEN")
            high_px = self._require_non_negative_decimal(row, "HIGH")
            low_px = self._require_non_negative_decimal(row, "LOW")
            close_px = self._require_non_negative_decimal(row, "CLOSE")
            self._validate_ohlc_bounds(open_px, high_px, low_px, close_px)
            validated.append(
                {
                    "symbol": self._require_text(row, "SC_NAME"),
                    "series": self._map_series(self._require_text(row, "SC_GROUP")),
                    "open": open_px,
                    "high": high_px,
                    "low": low_px,
                    "close": close_px,
                    "volume": self._require_non_negative_decimal(row, "NO_OF_SHRS"),
                    "delivery_qty": self._optional_non_negative_decimal(row, "DELIV_QTY"),
                    "timestamp": self._parse_trade_date(row["DT_TM"], trade_date),
                }
            )
        return validated

    def write_raw(
        self,
        rows: list[dict[str, object]],
        payload: bytes,
        trade_date: date,
        pipeline_run_id: int,
    ) -> RawWriteResult:
        return self._write_raw_payload(
            rows=rows,
            payload=payload,
            trade_date=trade_date,
            source_system=self.source_system,
        )

    @staticmethod
    def _build_url(trade_date: date) -> str:
        return (
            "https://www.bseindia.com/download/BhavCopy/Equity/"
            f"EQ{trade_date.strftime('%d%m%y')}_CSV.ZIP"
        )

    @staticmethod
    def _default_fetcher(url: str) -> bytes:
        with urlopen(url, timeout=30) as response:
            return response.read()

    @staticmethod
    def _require_text(row: dict[str, str], field: str) -> str:
        value = row.get(field, "").strip().upper()
        if not value:
            raise ValueError(f"{field} is required")
        return value

    @staticmethod
    def _require_non_negative_decimal(row: dict[str, str], field: str) -> Decimal:
        try:
            value = Decimal(row[field].strip())
        except (KeyError, InvalidOperation) as exc:
            raise ValueError(f"{field} must be numeric") from exc
        if value < 0:
            raise ValueError(f"{field} must be non-negative")
        return value

    @classmethod
    def _optional_non_negative_decimal(
        cls,
        row: dict[str, str],
        field: str,
    ) -> Decimal | None:
        raw_value = row.get(field)
        if raw_value is None or not raw_value.strip():
            return None
        return cls._require_non_negative_decimal(row, field)

    @staticmethod
    def _parse_trade_date(raw_value: str, trade_date: date) -> date:
        parsed = BseEodConnector._parse_date(raw_value)
        if parsed != trade_date:
            raise ValueError("DT_TM does not match requested trade date")
        return parsed

    @staticmethod
    def _parse_date(raw_value: str) -> date:
        cleaned = raw_value.strip()
        for parser in ("%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y", "%d/%m/%Y"):
            try:
                from datetime import datetime

                return datetime.strptime(cleaned, parser).date()
            except ValueError:
                continue
        raise ValueError(f"Unsupported trade date format: {raw_value}")

    @staticmethod
    def _decode_payload(payload: bytes) -> str:
        if payload[:2] != b"PK":
            return payload.decode("utf-8")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if not members:
                raise ValueError("BSE archive does not contain a CSV payload")
            return archive.read(members[0]).decode("utf-8")

    @staticmethod
    def _validate_ohlc_bounds(
        open_px: Decimal,
        high_px: Decimal,
        low_px: Decimal,
        close_px: Decimal,
    ) -> None:
        if high_px < max(open_px, low_px, close_px):
            raise ValueError("HIGH must be greater than or equal to OPEN, LOW, and CLOSE")
        if low_px > min(open_px, high_px, close_px):
            raise ValueError("LOW must be less than or equal to OPEN, HIGH, and CLOSE")

    @staticmethod
    def _map_series(series: str) -> str:
        if series not in BSE_SERIES_MAP:
            raise ValueError(f"Unsupported BSE series code: {series}")
        return BSE_SERIES_MAP[series]
