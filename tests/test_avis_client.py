from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("AVIS_BASE_URL", "http://avis.test")

from avis.client import (
    CompliancePolicyError,
    EntityNotFoundError,
    ValidationError,
)
from avis.client.instruments import InstrumentsClient
from avis.client.market import MarketClient
from avis.client.models import InstrumentDetail, InstrumentResult


class MockResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.content = b"" if payload is None else b"payload"

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class MockSession:
    def __init__(self, responses: list[MockResponse]) -> None:
        self.responses = responses
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict | None, dict | None]] = []

    def request(self, method: str, url: str, params=None, json=None, timeout=None):
        self.calls.append((method, url, params, json))
        return self.responses.pop(0)

    def get(self, url: str, params=None, timeout=None):
        self.calls.append(("GET", url, params, None))
        return self.responses.pop(0)


def test_retry_on_500(monkeypatch) -> None:
    session = MockSession([
        MockResponse(500, {"detail": {"message": "server error"}}),
        MockResponse(500, {"detail": {"message": "server error"}}),
        MockResponse(200, {"items": [], "pagination": {"page": 1, "page_size": 200, "total": 0}}),
        MockResponse(200, {"quotes": []}),
    ])
    monkeypatch.setattr("requests.Session", lambda: session)
    sleep_calls: list[float] = []
    monkeypatch.setattr("time.sleep", lambda seconds: sleep_calls.append(seconds))

    client = InstrumentsClient(base_url="http://avis.test", jwt_token="token")
    client.search("ITC")

    assert len(session.calls) == 4
    assert sleep_calls == [0.5, 1.0]


def test_compliance_policy_error_on_403(monkeypatch) -> None:
    session = MockSession([
        MockResponse(403, {"detail": {"message": "Response blocked by source redistribution policy", "attribution": "Source: NSE_EOD"}})
    ])
    monkeypatch.setattr("requests.Session", lambda: session)
    client = MarketClient(base_url="http://avis.test", jwt_token="token")

    with pytest.raises(CompliancePolicyError) as exc:
        client.ohlcv("abc", "2026-01-01", "2026-01-31")

    assert exc.value.attribution == "Source: NSE_EOD"


def test_entity_not_found_error_on_404(monkeypatch) -> None:
    session = MockSession([
        MockResponse(404, {"detail": {"message": "Unknown instrument_uuid"}})
    ])
    monkeypatch.setattr("requests.Session", lambda: session)
    client = InstrumentsClient(base_url="http://avis.test", jwt_token="token")

    with pytest.raises(EntityNotFoundError):
        client.get("missing")


def test_validation_error_on_422(monkeypatch) -> None:
    session = MockSession([
        MockResponse(422, {"detail": {"message": "from_date must be on or before to_date", "code": "invalid_date_range"}})
    ])
    monkeypatch.setattr("requests.Session", lambda: session)
    client = MarketClient(base_url="http://avis.test", jwt_token="token")

    with pytest.raises(ValidationError) as exc:
        client.ohlcv("abc", "2026-02-01", "2026-01-01")

    assert exc.value.detail["code"] == "invalid_date_range"


def test_fallback_to_none_when_avis_disabled(monkeypatch) -> None:
    session = MockSession([])
    monkeypatch.setattr("requests.Session", lambda: session)
    client = InstrumentsClient(base_url="http://avis.test", jwt_token="token", enabled=False)

    result = client.search("ITC")

    assert result is None
    assert session.calls == []


def test_price_history_dataframe_shape(monkeypatch) -> None:
    session = MockSession([
        MockResponse(
            200,
            {
                "instrument_uuid": "3f4e81f6-fb2d-4605-8c1f-4b50d99a6d4e",
                "adjusted": True,
                "rows": [
                    {
                        "trade_date": "2026-06-25",
                        "open_px": "100.000000",
                        "high_px": "101.000000",
                        "low_px": "99.000000",
                        "close_px": "100.000000",
                        "adjusted_close_px": "95.000000",
                        "volume": "1000",
                        "source_system": "INTERNAL_MODEL",
                    }
                ],
            },
        )
    ])
    monkeypatch.setattr("requests.Session", lambda: session)
    client = InstrumentsClient(base_url="http://avis.test", jwt_token="token")

    frame = client.price_history("uuid", "2026-06-25", "2026-06-25")

    assert list(frame.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert frame.shape == (1, 6)
    assert frame.iloc[0]["close"] == Decimal("95.000000")


def test_get_instrument_parses_dataclass(monkeypatch) -> None:
    session = MockSession([
        MockResponse(
            200,
            {
                "instrument_uuid": "3f4e81f6-fb2d-4605-8c1f-4b50d99a6d4e",
                "symbol": "ITC",
                "isin": "INE154A01025",
                "exchange": "NSE",
                "company_name": "ITC Ltd",
                "is_active": True,
                "sector": "FMCG",
                "aliases": [],
            },
        )
    ])
    monkeypatch.setattr("requests.Session", lambda: session)
    client = InstrumentsClient(base_url="http://avis.test", jwt_token="token")

    instrument = client.get("uuid")

    assert isinstance(instrument, InstrumentDetail)
    assert instrument.symbol == "ITC"


def test_search_falls_back_to_yahoo_when_avis_empty(monkeypatch) -> None:
    avis_session = MockSession([
        MockResponse(200, {"items": [], "pagination": {"page": 1, "page_size": 200, "total": 0}})
    ])
    yahoo_session = MockSession([
        MockResponse(200, {"quotes": [{"symbol": "ITC.NS", "exchange": "NSI", "shortname": "ITC Ltd"}]})
    ])
    sessions = [avis_session, yahoo_session]
    monkeypatch.setattr("requests.Session", lambda: sessions.pop(0))
    client = InstrumentsClient(base_url="http://avis.test", jwt_token="token")

    results = client.search("ITC")

    assert results == [InstrumentResult(instrument_uuid=None, symbol="ITC.NS", isin=None, exchange="NSI", company_name="ITC Ltd")]
