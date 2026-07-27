from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("AVIS_BASE_URL", "http://avis.test")

from avis.client import EntityNotFoundError
from avis.client.models import AttributionRow, ConfidenceSnapshot, InstrumentResult, ModelOutput, ValuationDetail, ValuationJob
from apps.terminal.data_router import DataRouter


class MockResponse:
    def __init__(self, payload: dict, status_code: int = 200, text: str = "") -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class MockSession:
    def __init__(self, payloads: list[MockResponse]) -> None:
        self.payloads = payloads

    def get(self, *args, **kwargs):
        return self.payloads.pop(0)


class FakeInstrumentsClient:
    def __init__(self, results=None):
        self.results = results or []
        self.payload = None

    def search(self, query: str):
        return self.results

    def _get(self, path: str, params=None):
        return self.payload


class FakeMarketClient:
    def __init__(self, frame: pd.DataFrame | None = None, exc: Exception | None = None) -> None:
        self.frame = frame
        self.exc = exc

    def ohlcv(self, instrument_uuid, from_date, to_date, adjusted=True):
        if self.exc is not None:
            raise self.exc
        return self.frame


class FakeValuationsClient:
    def __init__(self, job: ValuationJob, detail: ValuationDetail, attribution: list[AttributionRow]) -> None:
        self.job = job
        self.detail = detail
        self.attribution = attribution

    def run_valuation(self, *args, **kwargs):
        return self.job

    def get_valuation(self, val_run_id: int):
        return self.detail

    def get_attribution(self, val_run_id: int):
        return self.attribution

    def list_valuations(self, instrument_uuid: str):
        return [
            SimpleNamespace(
                val_run_id=self.detail.val_run_id,
                run_uuid=self.detail.run_uuid,
                as_of_ts=self.detail.as_of_ts,
                status=self.detail.status,
                run_label=self.detail.run_label,
                blended_value=Decimal("150.50"),
                dcf_value=Decimal("148.20"),
                relative_value=None,
                confidence_score=Decimal("72"),
            )
        ]


class FakeIncidentResolution:
    def __init__(self, incident_id: int, override_id: int, incident_status: str, resolution_notes: str) -> None:
        self.incident_id = incident_id
        self.override_id = override_id
        self.incident_status = incident_status
        self.resolution_notes = resolution_notes


class FakeQualityClient:
    def resolve_incident(self, incident_id: int, justification: str, analyst_id: str):
        return FakeIncidentResolution(incident_id, 9, "RESOLVED", justification)


class FakeTicker:
    def __init__(self, history_frame: pd.DataFrame | None = None, info: dict | None = None) -> None:
        self._history_frame = history_frame if history_frame is not None else pd.DataFrame()
        self.info = info or {}
        self.financials = pd.DataFrame()
        self.balance_sheet = pd.DataFrame()
        self.cashflow = pd.DataFrame()
        self.upgrades_downgrades = pd.DataFrame()

    def history(self, *args, **kwargs):
        return self._history_frame.copy()


class FakeYFinance:
    def __init__(self, ticker: FakeTicker) -> None:
        self._ticker = ticker

    def Ticker(self, symbol: str):
        return self._ticker


def test_resolve_ticker_avis_hit_returns_instrument_uuid() -> None:
    router = DataRouter(
        avis_enabled=True,
        instruments_client=FakeInstrumentsClient(
            [
                InstrumentResult(
                    instrument_uuid=uuid4(),
                    symbol="ITC.NS",
                    isin="INE154A01025",
                    exchange="NSE",
                    company_name="ITC Ltd",
                )
            ]
        ),
    )

    result = router.resolve_ticker("ITC")

    assert result["symbol"] == "ITC.NS"
    assert result["instrument_uuid"] is not None


def test_resolve_ticker_avis_miss_falls_back_to_yahoo_search() -> None:
    router = DataRouter(
        avis_enabled=True,
        instruments_client=FakeInstrumentsClient([]),
        requests_session=MockSession(
            [MockResponse({"quotes": [{"symbol": "ITC.NS", "shortname": "ITC Ltd", "exchange": "NSI"}]})]
        ),
    )

    result = router.resolve_ticker("ITC")

    assert result == {
        "symbol": "ITC.NS",
        "name": "ITC Ltd",
        "exchange": "NSI",
        "instrument_uuid": None,
    }


def test_price_history_avis_returns_dataframe_with_correct_columns() -> None:
    frame = pd.DataFrame(
        {
            "date": [pd.Timestamp("2026-07-01")],
            "open": [100.0],
            "high": [101.0],
            "low": [99.5],
            "close": [100.5],
            "volume": [1000],
        }
    )
    router = DataRouter(avis_enabled=True, market_client=FakeMarketClient(frame=frame))

    result = router.price_history("ITC.NS", instrument_uuid="abc")

    assert list(result.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert result.iloc[0]["close"] == 100.5


def test_price_history_avis_404_falls_back_to_yfinance() -> None:
    yf_history = pd.DataFrame(
        {
            "Open": [200.0],
            "High": [202.0],
            "Low": [198.0],
            "Close": [201.0],
            "Volume": [5000],
        },
        index=pd.DatetimeIndex([pd.Timestamp("2026-07-02")], name="Date"),
    )
    router = DataRouter(
        avis_enabled=True,
        market_client=FakeMarketClient(exc=EntityNotFoundError("missing")),
        yfinance_module=FakeYFinance(FakeTicker(history_frame=yf_history)),
    )

    result = router.price_history("ITC.NS", instrument_uuid="missing")

    assert list(result.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert result.iloc[0]["close"] == 201.0


def test_run_dcf_valuation_avis_returns_blended_value() -> None:
    run_uuid = uuid4()
    instrument_uuid = uuid4()
    job = ValuationJob(
        val_run_id=1,
        run_uuid=run_uuid,
        status="QUEUED",
        queued_at=datetime(2026, 7, 27, 10, 0, 0),
        blended_value=None,
        dcf_value=None,
        relative_value=None,
        confidence_score=None,
        override_required=None,
    )
    detail = ValuationDetail(
        val_run_id=1,
        run_uuid=run_uuid,
        instrument_uuid=instrument_uuid,
        status="SUCCESS",
        as_of_ts=datetime(2026, 7, 27, 10, 1, 0),
        run_type="MANUAL",
        trigger_type="API",
        created_at=datetime(2026, 7, 27, 10, 0, 0),
        completed_at=datetime(2026, 7, 27, 10, 1, 0),
        run_label="streamlit-client",
        assumption_set=[],
        model_outputs=[
            ModelOutput(
                model_output_id=1,
                model_name="BLENDED",
                model_version="v1",
                equity_value=Decimal("1000"),
                enterprise_value=Decimal("1200"),
                target_price=Decimal("150.50"),
                weight=Decimal("1"),
                currency_code="INR",
                output_payload=None,
                created_at=datetime(2026, 7, 27, 10, 1, 0),
            ),
            ModelOutput(
                model_output_id=2,
                model_name="DCF",
                model_version="v1",
                equity_value=Decimal("1000"),
                enterprise_value=Decimal("1200"),
                target_price=Decimal("148.20"),
                weight=Decimal("0.5"),
                currency_code="INR",
                output_payload=None,
                created_at=datetime(2026, 7, 27, 10, 1, 0),
            ),
        ],
        attributions=[],
        confidence_snapshots=[
            ConfidenceSnapshot(
                confidence_id=1,
                overall_confidence=Decimal("72"),
                data_quality_score=Decimal("75"),
                management_credibility_score=Decimal("70"),
                industry_stability_score=Decimal("68"),
                forecast_accuracy_score=Decimal("71"),
                news_reliability_score=Decimal("74"),
                model_agreement_score=Decimal("73"),
                scenario_dispersion_score=Decimal("27"),
                scoring_version="v1",
                created_at=datetime(2026, 7, 27, 10, 1, 0),
            )
        ],
    )
    attribution = [
        AttributionRow(
            attribution_id=1,
            driver_type="FCF",
            driver_key="year_1",
            impact_value_abs=Decimal("20"),
            impact_value_pct=Decimal("0.1"),
            direction="UP",
            evidence_ref=None,
            created_at=datetime(2026, 7, 27, 10, 1, 0),
        )
    ]
    router = DataRouter(
        avis_enabled=True,
        valuations_client=FakeValuationsClient(job=job, detail=detail, attribution=attribution),
    )

    result = router.run_dcf_valuation(
        str(instrument_uuid),
        wacc=0.10,
        terminal_growth=0.04,
        forecast_years=5,
        revenue_cagr=0.08,
        ebit_margin=0.18,
        symbol="ITC.NS",
        poll_interval=0.0,
    )

    assert result is not None
    assert result["source"] == "avis"
    assert result["blended_value"] == 150.5
    assert result["dcf_value"] == 148.2
    assert result["confidence_score"] == 72.0


def test_run_dcf_valuation_avis_disabled_returns_local_dcf_result() -> None:
    router = DataRouter(avis_enabled=False)
    router._local_dcf_valuation = lambda **kwargs: {  # type: ignore[method-assign]
        "source": "local",
        "blended_value": 123.0,
        "dcf_value": 123.0,
        "relative_value": None,
        "confidence_score": None,
        "override_required": False,
        "attribution_rows": [],
    }

    result = router.run_dcf_valuation(
        None,
        wacc=0.10,
        terminal_growth=0.04,
        forecast_years=5,
        revenue_cagr=0.08,
        ebit_margin=0.18,
        symbol="ITC.NS",
    )

    assert result is not None
    assert result["source"] == "local"
    assert result["dcf_value"] == 123.0


def test_dq_incidents_avis_offline_returns_none_gracefully() -> None:
    router = DataRouter(avis_enabled=False)

    assert router.dq_incidents() is None


def test_resolve_incident_returns_override_payload(monkeypatch) -> None:
    monkeypatch.setenv("AVIS_ANALYST_ID", str(uuid4()))
    router = DataRouter(avis_enabled=True, quality_client=FakeQualityClient())

    result = router.resolve_incident(7, "This override is justified by confirmed vendor remediation.")

    assert result is not None
    assert result["incident_id"] == 7
    assert result["incident_status"] == "RESOLVED"


def test_valuation_run_browser_aggregates_runs() -> None:
    run_uuid = uuid4()
    instrument_uuid = uuid4()
    job = ValuationJob(
        val_run_id=11,
        run_uuid=run_uuid,
        status="QUEUED",
        queued_at=datetime(2026, 7, 27, 10, 0, 0),
        blended_value=None,
        dcf_value=None,
        relative_value=None,
        confidence_score=None,
        override_required=None,
    )
    detail = ValuationDetail(
        val_run_id=11,
        run_uuid=run_uuid,
        instrument_uuid=instrument_uuid,
        status="OVERRIDDEN",
        as_of_ts=datetime(2026, 7, 27, 10, 1, 0),
        run_type="MANUAL",
        trigger_type="API",
        created_at=datetime(2026, 7, 27, 10, 0, 0),
        completed_at=datetime(2026, 7, 27, 10, 1, 0),
        run_label="streamlit-client",
        assumption_set=[],
        model_outputs=[],
        attributions=[],
        confidence_snapshots=[],
    )
    instruments = FakeInstrumentsClient()
    instruments.payload = {
        "items": [
            {
                "instrument_uuid": str(instrument_uuid),
                "symbol": "ITC.NS",
                "isin": "INE154A01025",
                "exchange": "NSE",
                "company_name": "ITC Ltd",
            }
        ],
        "pagination": {"page": 1, "page_size": 200, "total": 1},
    }
    router = DataRouter(
        avis_enabled=True,
        instruments_client=instruments,
        valuations_client=FakeValuationsClient(job=job, detail=detail, attribution=[]),
    )

    frame = router.valuation_run_browser()

    assert frame is not None
    assert not frame.empty
    assert frame.iloc[0]["instrument"] == "ITC.NS"
    assert bool(frame.iloc[0]["override_required"]) is True
