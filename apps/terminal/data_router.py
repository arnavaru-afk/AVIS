"""AVIS-aware data routing for the Global Market Terminal."""

from __future__ import annotations

import os
import re
import time
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable
from urllib.parse import quote_plus
from xml.etree import ElementTree

import pandas as pd
import requests

from avis.client import (
    CompliancePolicyError,
    EntityNotFoundError,
    InstrumentsClient,
    MarketClient,
    PipelineClient,
    QualityClient,
    ValuationsClient,
)
from avis.client.config import ClientConfig
from avis.client.models import (
    AttributionRow,
    ConfidenceSnapshot,
    IncidentSummary,
    PipelineRun,
    ValuationDetail,
    ValuationJob,
)

try:
    import yfinance as yf
except ImportError:  # pragma: no cover - exercised through dependency-missing fallback.
    yf = None

def _env_flag(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


AVIS_ENABLED = _env_flag(os.getenv("AVIS_ENABLED"), default=True)
YAHOO_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"
PENDING_STATUSES = {"QUEUED", "RUNNING", "PENDING"}
TERMINAL_FAILURE_STATUSES = {"FAILED", "ERROR"}


class DataRouter:
    """Route terminal data requests through AVIS first, then public fallbacks."""

    def __init__(
        self,
        *,
        avis_enabled: bool | None = None,
        instruments_client: InstrumentsClient | None = None,
        market_client: MarketClient | None = None,
        valuations_client: ValuationsClient | None = None,
        quality_client: QualityClient | None = None,
        pipeline_client: PipelineClient | None = None,
        requests_session: requests.Session | None = None,
        yfinance_module: Any | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        config = ClientConfig.from_env()
        self.avis_enabled = config.enabled if avis_enabled is None else avis_enabled
        self.avis = None
        self.instruments = instruments_client
        self.market = market_client
        self.valuations = valuations_client
        self.quality = quality_client
        self.pipeline = pipeline_client
        if self.avis_enabled:
            self.avis = instruments_client or market_client or valuations_client or quality_client or pipeline_client or InstrumentsClient()
            self.instruments = self.instruments or InstrumentsClient(enabled=True)
            self.market = self.market or MarketClient(enabled=True)
            self.valuations = self.valuations or ValuationsClient(enabled=True)
            self.quality = self.quality or QualityClient(enabled=True)
            self.pipeline = self.pipeline or PipelineClient(enabled=True)
        self.session = requests_session or requests.Session()
        self.yf = yfinance_module if yfinance_module is not None else yf
        self.sleep_fn = sleep_fn or time.sleep

    def resolve_ticker(self, query: str) -> dict[str, Any]:
        normalized = query.strip()
        if not normalized:
            return {}

        if self.avis_enabled and self.instruments is not None:
            try:
                results = self.instruments.search(normalized) or []
            except Exception:
                results = []
            if results:
                top = results[0]
                return {
                    "symbol": top.symbol,
                    "name": top.company_name,
                    "exchange": top.exchange,
                    "instrument_uuid": str(top.instrument_uuid) if top.instrument_uuid is not None else None,
                }

        for item in self._yahoo_search(normalized):
            return item
        return {"symbol": normalized, "name": normalized, "exchange": None, "instrument_uuid": None}

    def price_history(
        self,
        symbol: str,
        instrument_uuid: str | None = None,
        *,
        period: str = "1y",
        from_date: str | date | None = None,
        to_date: str | date | None = None,
        adjusted: bool = True,
    ) -> pd.DataFrame:
        start_date, end_date = self._normalize_dates(period=period, from_date=from_date, to_date=to_date)
        if instrument_uuid and self.avis_enabled and self.market is not None:
            try:
                frame = self.market.ohlcv(instrument_uuid, start_date, end_date, adjusted=adjusted)
                if frame is not None and not frame.empty:
                    return self._normalize_history_frame(frame)
            except (CompliancePolicyError, EntityNotFoundError, ValueError, requests.RequestException):
                pass
            except Exception:
                pass

        history = self._yfinance_history(symbol, period=period, from_date=start_date, to_date=end_date, adjusted=adjusted)
        return self._normalize_history_frame(history)

    def instrument_info(self, symbol: str, instrument_uuid: str | None = None) -> dict[str, Any]:
        if instrument_uuid and self.avis_enabled and self.instruments is not None:
            try:
                detail = self.instruments.get(instrument_uuid)
                if detail is not None:
                    return {
                        "instrument_uuid": str(detail.instrument_uuid),
                        "symbol": detail.symbol,
                        "isin": detail.isin,
                        "exchange": detail.exchange,
                        "company_name": detail.company_name,
                        "is_active": detail.is_active,
                        "sector": detail.sector,
                        "aliases": [asdict(alias) for alias in detail.aliases],
                    }
            except (CompliancePolicyError, EntityNotFoundError, ValueError, requests.RequestException):
                pass
            except Exception:
                pass

        info = self._yfinance_info(symbol)
        if not info:
            return {"symbol": symbol, "company_name": symbol}
        normalized = {self._snake_case(key): value for key, value in info.items()}
        normalized.setdefault("symbol", symbol)
        normalized.setdefault("company_name", normalized.get("long_name") or normalized.get("short_name") or symbol)
        return normalized

    def run_dcf_valuation(
        self,
        instrument_uuid: str | None,
        wacc: float | Decimal,
        terminal_growth: float | Decimal,
        forecast_years: int,
        revenue_cagr: float | Decimal,
        ebit_margin: float | Decimal,
        *,
        symbol: str | None = None,
        poll_interval: float = 0.5,
        max_polls: int = 20,
    ) -> dict[str, Any] | None:
        if instrument_uuid and self.avis_enabled and self.valuations is not None:
            try:
                job = self.valuations.run_valuation(
                    instrument_uuid,
                    wacc=wacc,
                    terminal_growth=terminal_growth,
                    forecast_years=forecast_years,
                    revenue_cagr=revenue_cagr,
                    ebit_margin=ebit_margin,
                )
                if job is not None:
                    result = self._poll_avis_valuation(job, max_polls=max_polls, poll_interval=poll_interval)
                    if result is not None:
                        return result
            except (CompliancePolicyError, EntityNotFoundError, ValueError, requests.RequestException):
                pass
            except Exception:
                pass

        if symbol is None:
            return None
        return self._local_dcf_valuation(
            symbol=symbol,
            wacc=wacc,
            terminal_growth=terminal_growth,
            forecast_years=forecast_years,
            revenue_cagr=revenue_cagr,
            ebit_margin=ebit_margin,
        )

    def valuation_history(self, instrument_uuid: str | None) -> list[dict[str, Any]] | None:
        if not instrument_uuid or not self.avis_enabled or self.valuations is None:
            return None
        try:
            rows = self.valuations.list_valuations(instrument_uuid)
        except Exception:
            return None
        if rows is None:
            return None
        history: list[dict[str, Any]] = []
        for row in rows:
            history.append(
                {
                    "val_run_id": row.val_run_id,
                    "run_uuid": str(row.run_uuid),
                    "as_of_ts": row.as_of_ts,
                    "status": row.status,
                    "run_label": row.run_label,
                    "blended_value": self._to_float(row.blended_value),
                    "dcf_value": self._to_float(row.dcf_value),
                    "relative_value": self._to_float(row.relative_value),
                    "confidence_score": self._to_float(row.confidence_score),
                }
            )
        return history

    def peer_multiples(self, peers: list[str]) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for peer in peers:
            info = self._yfinance_info(peer)
            if not info:
                continue
            market_cap = self._numeric(info.get("marketCap"))
            enterprise_value = self._numeric(info.get("enterpriseValue"))
            ebitda = self._numeric(info.get("ebitda"))
            book_value = self._numeric(info.get("bookValue"))
            sales = self._numeric(info.get("totalRevenue"))
            price = self._numeric(info.get("currentPrice") or info.get("regularMarketPrice"))
            rows.append(
                {
                    "symbol": peer,
                    "company_name": info.get("longName") or info.get("shortName") or peer,
                    "p_e": self._safe_ratio(price, self._numeric(info.get("trailingEps"))),
                    "ev_ebitda": self._safe_ratio(enterprise_value, ebitda),
                    "p_b": self._safe_ratio(price, book_value),
                    "p_s": self._safe_ratio(market_cap, sales),
                }
            )
        return pd.DataFrame(rows)

    def dq_incidents(self) -> list[dict[str, Any]] | None:
        if not self.avis_enabled or self.quality is None:
            return None
        try:
            incidents = self.quality.list_incidents()
        except Exception:
            return None
        if incidents is None:
            return None
        detailed_incidents: list[dict[str, Any]] = []
        for item in incidents:
            payload = self._incident_to_dict(item)
            try:
                detail = self.quality.get_incident(item.incident_id)
            except Exception:
                detail = None
            if detail is not None:
                payload["target_record_key"] = detail.dq_result.target_record_key
                payload["target_table"] = detail.dq_result.target_table
                payload["failure_reason"] = detail.dq_result.failure_reason
            detailed_incidents.append(payload)
        return detailed_incidents

    def resolve_incident(self, incident_id: int, justification: str, analyst_id: str | None = None) -> dict[str, Any] | None:
        if not self.avis_enabled or self.quality is None:
            return None
        analyst = analyst_id or os.getenv("AVIS_ANALYST_ID")
        if not analyst or len(justification.strip()) < 20:
            return None
        try:
            response = self.quality.resolve_incident(incident_id, justification.strip(), analyst)
        except Exception:
            return None
        if response is None:
            return None
        return {
            "incident_id": response.incident_id,
            "override_id": response.override_id,
            "incident_status": response.incident_status,
            "resolution_notes": response.resolution_notes,
        }

    def pipeline_status(self) -> list[dict[str, Any]] | None:
        if not self.avis_enabled or self.pipeline is None:
            return None
        try:
            runs = self.pipeline.list_runs()
        except Exception:
            return None
        if runs is None:
            return None
        return [self._pipeline_run_to_dict(item) for item in runs]

    def pipeline_run_detail(self, run_id: int) -> dict[str, Any] | None:
        if not self.avis_enabled or self.pipeline is None:
            return None
        try:
            detail = self.pipeline.get_run(run_id)
        except Exception:
            return None
        if detail is None:
            return None
        return {
            "pipeline_run_id": detail.pipeline_run_id,
            "run_uuid": str(detail.run_uuid),
            "pipeline_name": detail.pipeline_name,
            "run_mode": detail.run_mode,
            "triggered_by": detail.triggered_by,
            "status": detail.status,
            "started_at": detail.started_at,
            "ended_at": detail.ended_at,
            "run_context": detail.run_context,
            "sla_breach": detail.sla_breach,
            "job_events": [
                {
                    "job_event_id": event.job_event_id,
                    "job_name": event.job_name,
                    "event_type": event.event_type,
                    "event_ts": event.event_ts,
                    "message": event.message,
                    "metrics_payload": event.metrics_payload,
                }
                for event in detail.job_events
            ],
        }

    def list_instruments(self, exchange: str | None = None) -> list[dict[str, Any]] | None:
        if not self.avis_enabled or self.instruments is None:
            return None
        page = 1
        page_size = 200
        items: list[dict[str, Any]] = []
        total = None
        try:
            while total is None or len(items) < total:
                params: dict[str, Any] = {"page": page, "page_size": page_size}
                if exchange:
                    params["exchange"] = exchange
                payload = self.instruments._get("/api/v1/instruments", params=params)
                if payload is None:
                    break
                total = int(payload["pagination"]["total"])
                for item in payload["items"]:
                    items.append(
                        {
                            "instrument_uuid": str(item["instrument_uuid"]),
                            "symbol": item["symbol"],
                            "isin": item.get("isin"),
                            "exchange": item.get("exchange"),
                            "company_name": item.get("company_name"),
                        }
                    )
                if (page * page_size) >= total:
                    break
                page += 1
        except Exception:
            return None
        return items

    def coverage_map(self, exchange: str | None = None) -> pd.DataFrame | None:
        instruments = self.list_instruments(exchange=exchange)
        if instruments is None:
            return None
        rows: list[dict[str, Any]] = []
        lookback_start = date.today() - timedelta(days=3650)
        today = date.today()
        for instrument in instruments:
            instrument_uuid = instrument["instrument_uuid"]
            last_val_date = None
            valuations = self.valuation_history(instrument_uuid)
            if valuations:
                last_val_date = valuations[0].get("as_of_ts")
            last_price_date = None
            if self.market is not None:
                try:
                    price_frame = self.market.ohlcv(instrument_uuid, lookback_start, today, adjusted=True)
                    if price_frame is not None and not price_frame.empty:
                        last_price_date = pd.to_datetime(price_frame["date"]).max()
                except Exception:
                    last_price_date = None
            rows.append(
                {
                    "symbol": instrument["symbol"],
                    "isin": instrument["isin"],
                    "exchange": instrument["exchange"],
                    "instrument_uuid": instrument_uuid,
                    "last_price_date": last_price_date,
                    "last_val_date": last_val_date,
                    "valued": last_val_date is not None,
                }
            )
        return pd.DataFrame(rows)

    def valuation_run_browser(self, exchange: str | None = None) -> pd.DataFrame | None:
        instruments = self.list_instruments(exchange=exchange)
        if instruments is None:
            return None
        rows: list[dict[str, Any]] = []
        for instrument in instruments:
            history = self.valuation_history(instrument["instrument_uuid"]) or []
            for run in history:
                rows.append(
                    {
                        "instrument_uuid": instrument["instrument_uuid"],
                        "instrument": instrument["symbol"],
                        "exchange": instrument["exchange"],
                        "val_run_id": run["val_run_id"],
                        "run_date": run["as_of_ts"],
                        "blended_value": run["blended_value"],
                        "confidence_score": run["confidence_score"],
                        "override_required": run["status"] == "OVERRIDDEN" or (
                            run["confidence_score"] is not None and float(run["confidence_score"]) < 40
                        ),
                        "status": run["status"],
                    }
                )
        if not rows:
            return pd.DataFrame(
                columns=[
                    "instrument_uuid",
                    "instrument",
                    "exchange",
                    "val_run_id",
                    "run_date",
                    "blended_value",
                    "confidence_score",
                    "override_required",
                    "status",
                ]
            )
        frame = pd.DataFrame(rows)
        return frame.sort_values(["run_date", "val_run_id"], ascending=[False, False]).reset_index(drop=True)

    def valuation_run_detail(self, val_run_id: int) -> dict[str, Any] | None:
        if not self.avis_enabled or self.valuations is None:
            return None
        try:
            detail = self.valuations.get_valuation(val_run_id)
            attribution = self.valuations.get_attribution(val_run_id) or []
        except Exception:
            return None
        if detail is None:
            return None
        outputs = {row.model_name.upper(): row for row in detail.model_outputs}
        confidence = detail.confidence_snapshots[-1] if detail.confidence_snapshots else None
        return {
            "val_run_id": detail.val_run_id,
            "instrument_uuid": str(detail.instrument_uuid),
            "status": detail.status,
            "run_label": detail.run_label,
            "run_date": detail.as_of_ts,
            "blended_value": self._to_float(outputs.get("BLENDED").target_price if outputs.get("BLENDED") else None),
            "dcf_value": self._to_float(outputs.get("DCF").target_price if outputs.get("DCF") else None),
            "relative_value": self._to_float(outputs.get("RELATIVE").target_price if outputs.get("RELATIVE") else None),
            "confidence_score": self._confidence_value(confidence),
            "override_required": detail.status == "OVERRIDDEN" or self._override_required(confidence),
            "attribution_rows": [self._attribution_to_dict(row) for row in attribution],
        }

    def avis_status(self) -> dict[str, Any]:
        if not self.avis_enabled or self.instruments is None:
            return {"status": "offline", "message": "falling back to yfinance for all data"}
        try:
            payload = self.instruments._get("/api/v1/instruments", params={"page": 1, "page_size": 1})
            total = int(payload["pagination"]["total"])
            runs = self.pipeline_status() or []
        except Exception:
            return {"status": "offline", "message": "falling back to yfinance for all data"}

        last_ts = None
        if runs:
            timestamps = [run.get("ended_at") or run.get("started_at") for run in runs if run.get("ended_at") or run.get("started_at")]
            if timestamps:
                last_ts = max(timestamps)
        if last_ts is None:
            return {"status": "degraded", "instrument_count": total, "last_pipeline_run": None}
        age = datetime.now(UTC).replace(tzinfo=None) - last_ts.replace(tzinfo=None) if isinstance(last_ts, datetime) else timedelta.max
        status = "connected" if age <= timedelta(hours=24) else "degraded"
        return {"status": status, "instrument_count": total, "last_pipeline_run": last_ts}

    def company_news(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        try:
            response = self.session.get(
                f"{GOOGLE_NEWS_RSS_URL}?q={quote_plus(query)}&hl=en-IN&gl=IN&ceid=IN:en",
                timeout=20,
            )
            response.raise_for_status()
            root = ElementTree.fromstring(response.text)
        except Exception:
            return []
        items: list[dict[str, Any]] = []
        for item in root.findall(".//item")[:limit]:
            title = item.findtext("title") or ""
            link = item.findtext("link") or ""
            pub_date = item.findtext("pubDate") or ""
            source = item.findtext("source") or "Google News"
            score = self._keyword_score(title)
            items.append(
                {
                    "title": title,
                    "link": link,
                    "published": pub_date,
                    "source": source,
                    "keyword_score": score,
                }
            )
        return items

    def analyst_actions(self, symbol: str) -> pd.DataFrame:
        ticker = self._yfinance_ticker(symbol)
        if ticker is None:
            return pd.DataFrame()
        upgrades = getattr(ticker, "upgrades_downgrades", None)
        if upgrades is None and hasattr(ticker, "get_upgrades_downgrades"):
            upgrades = ticker.get_upgrades_downgrades()
        if upgrades is None or getattr(upgrades, "empty", True):
            return pd.DataFrame()
        frame = upgrades.reset_index()
        frame.columns = [self._snake_case(str(column)) for column in frame.columns]
        return frame

    def financial_statements(self, symbol: str) -> dict[str, pd.DataFrame]:
        ticker = self._yfinance_ticker(symbol)
        if ticker is None:
            return {"income_statement": pd.DataFrame(), "balance_sheet": pd.DataFrame(), "cashflow": pd.DataFrame()}
        return {
            "income_statement": self._statement_frame(getattr(ticker, "financials", None)),
            "balance_sheet": self._statement_frame(getattr(ticker, "balance_sheet", None)),
            "cashflow": self._statement_frame(getattr(ticker, "cashflow", None)),
        }

    def macro_series(self, symbols: dict[str, str], *, period: str = "1y") -> dict[str, pd.DataFrame]:
        series: dict[str, pd.DataFrame] = {}
        for label, ticker_symbol in symbols.items():
            series[label] = self.price_history(ticker_symbol, period=period)
        return series

    def local_dcf_valuation(
        self,
        symbol: str,
        *,
        wacc: float | Decimal,
        terminal_growth: float | Decimal,
        forecast_years: int,
        revenue_cagr: float | Decimal,
        ebit_margin: float | Decimal,
    ) -> dict[str, Any]:
        return self._local_dcf_valuation(
            symbol=symbol,
            wacc=wacc,
            terminal_growth=terminal_growth,
            forecast_years=forecast_years,
            revenue_cagr=revenue_cagr,
            ebit_margin=ebit_margin,
        )

    def _poll_avis_valuation(self, job: ValuationJob, *, max_polls: int, poll_interval: float) -> dict[str, Any] | None:
        detail: ValuationDetail | None = None
        for _ in range(max_polls):
            detail = self.valuations.get_valuation(job.val_run_id) if self.valuations is not None else None
            if detail is None:
                break
            if detail.status not in PENDING_STATUSES:
                break
            self.sleep_fn(poll_interval)
        if detail is None:
            return None
        if detail.status in TERMINAL_FAILURE_STATUSES:
            return None

        attribution_rows = []
        if self.valuations is not None:
            try:
                attribution_rows = self.valuations.get_attribution(job.val_run_id) or []
            except Exception:
                attribution_rows = []

        outputs = {row.model_name.upper(): row for row in detail.model_outputs}
        confidence = detail.confidence_snapshots[-1] if detail.confidence_snapshots else None
        override_required = detail.status == "OVERRIDDEN" or self._override_required(confidence)
        return {
            "source": "avis",
            "val_run_id": detail.val_run_id,
            "status": detail.status,
            "blended_value": self._to_float(outputs.get("BLENDED").target_price if outputs.get("BLENDED") else None),
            "dcf_value": self._to_float(outputs.get("DCF").target_price if outputs.get("DCF") else None),
            "relative_value": self._to_float(outputs.get("RELATIVE").target_price if outputs.get("RELATIVE") else None),
            "confidence_score": self._confidence_value(confidence),
            "override_required": override_required,
            "attribution_rows": [self._attribution_to_dict(row) for row in attribution_rows],
        }

    def _local_dcf_valuation(
        self,
        *,
        symbol: str,
        wacc: float | Decimal,
        terminal_growth: float | Decimal,
        forecast_years: int,
        revenue_cagr: float | Decimal,
        ebit_margin: float | Decimal,
    ) -> dict[str, Any]:
        info = self.instrument_info(symbol)
        revenue = self._numeric(info.get("total_revenue")) or self._estimate_revenue_from_info(info)
        shares = self._numeric(info.get("shares_outstanding")) or 1.0
        tax_rate = self._numeric(info.get("effective_tax_rate")) or 0.25
        da_margin = self._numeric(info.get("depreciation_amortization"))
        da_margin = (da_margin / revenue) if revenue and da_margin else 0.03
        capex_margin = abs(self._numeric(info.get("capital_expenditure")) or 0.0)
        capex_margin = (capex_margin / revenue) if revenue and capex_margin else 0.04
        nwc_margin = 0.01
        net_debt = self._numeric(info.get("total_debt")) - self._numeric(info.get("total_cash"))

        wacc_value = float(wacc)
        growth_value = float(terminal_growth)
        revenue_growth = float(revenue_cagr)
        margin_value = float(ebit_margin)

        projected_revenue = revenue or 1.0
        pv_total = 0.0
        attribution_rows: list[dict[str, Any]] = []
        for year in range(1, int(forecast_years) + 1):
            projected_revenue *= 1.0 + revenue_growth
            ebit = projected_revenue * margin_value
            fcf = ebit * (1.0 - tax_rate) + projected_revenue * da_margin - projected_revenue * capex_margin - projected_revenue * nwc_margin
            discount_factor = (1.0 + wacc_value) ** year
            present_value = fcf / discount_factor
            pv_total += present_value
            attribution_rows.append(
                {
                    "year": year,
                    "revenue": projected_revenue,
                    "ebit": ebit,
                    "fcf": fcf,
                    "present_value": present_value,
                }
            )

        terminal_fcf = attribution_rows[-1]["fcf"] * (1.0 + growth_value)
        denominator = max(wacc_value - growth_value, 0.01)
        terminal_value = terminal_fcf / denominator
        discounted_terminal = terminal_value / ((1.0 + wacc_value) ** int(forecast_years))
        enterprise_value = pv_total + discounted_terminal
        equity_value = enterprise_value - net_debt
        intrinsic_value = equity_value / max(shares, 1.0)
        attribution_rows.append(
            {
                "year": "terminal",
                "revenue": attribution_rows[-1]["revenue"],
                "ebit": attribution_rows[-1]["ebit"],
                "fcf": terminal_fcf,
                "present_value": discounted_terminal,
            }
        )
        return {
            "source": "local",
            "status": "SUCCESS",
            "blended_value": intrinsic_value,
            "dcf_value": intrinsic_value,
            "relative_value": None,
            "confidence_score": None,
            "override_required": False,
            "attribution_rows": attribution_rows,
        }

    def _yfinance_history(
        self,
        symbol: str,
        *,
        period: str,
        from_date: date,
        to_date: date,
        adjusted: bool,
    ) -> pd.DataFrame:
        ticker = self._yfinance_ticker(symbol)
        if ticker is None:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        if from_date and to_date:
            history = ticker.history(start=from_date.isoformat(), end=(to_date + timedelta(days=1)).isoformat(), auto_adjust=adjusted)
        else:
            history = ticker.history(period=period, auto_adjust=adjusted)
        if history is None:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        return history

    def _yfinance_info(self, symbol: str) -> dict[str, Any]:
        ticker = self._yfinance_ticker(symbol)
        if ticker is None:
            return {}
        try:
            return dict(getattr(ticker, "info", {}) or {})
        except Exception:
            return {}

    def _yfinance_ticker(self, symbol: str) -> Any | None:
        if self.yf is None:
            return None
        return self.yf.Ticker(symbol)

    def _yahoo_search(self, query: str) -> list[dict[str, Any]]:
        try:
            response = self.session.get(
                YAHOO_SEARCH_URL,
                params={"q": query, "quotesCount": 10, "newsCount": 0},
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return []
        results: list[dict[str, Any]] = []
        for item in payload.get("quotes", []):
            symbol = item.get("symbol")
            if not symbol:
                continue
            results.append(
                {
                    "symbol": symbol,
                    "name": item.get("shortname") or item.get("longname") or symbol,
                    "exchange": item.get("exchange") or item.get("exchDisp"),
                    "instrument_uuid": None,
                }
            )
        return results

    @staticmethod
    def _normalize_dates(*, period: str, from_date: str | date | None, to_date: str | date | None) -> tuple[date, date]:
        end_date = DataRouter._coerce_date(to_date) or date.today()
        start_date = DataRouter._coerce_date(from_date)
        if start_date is not None:
            return start_date, end_date
        period_lower = period.lower()
        if period_lower.endswith("y"):
            years = int(period_lower[:-1] or 1)
            return end_date - timedelta(days=365 * years), end_date
        if period_lower.endswith("mo"):
            months = int(period_lower[:-2] or 1)
            return end_date - timedelta(days=30 * months), end_date
        if period_lower.endswith("d"):
            days = int(period_lower[:-1] or 1)
            return end_date - timedelta(days=days), end_date
        return end_date - timedelta(days=365), end_date

    @staticmethod
    def _coerce_date(value: str | date | None) -> date | None:
        if value is None:
            return None
        if isinstance(value, date):
            return value
        return datetime.fromisoformat(value).date()

    @staticmethod
    def _normalize_history_frame(frame: pd.DataFrame | None) -> pd.DataFrame:
        if frame is None or frame.empty:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
        normalized = frame.copy()
        if "date" not in normalized.columns:
            normalized = normalized.reset_index()
        rename_map = {column: str(column).strip().lower() for column in normalized.columns}
        normalized = normalized.rename(columns=rename_map)
        if "adj close" in normalized.columns and "close" not in normalized.columns:
            normalized = normalized.rename(columns={"adj close": "close"})
        if "adj close" in normalized.columns and "close" in normalized.columns:
            normalized["close"] = normalized["close"].fillna(normalized["adj close"])
        if "datetime" in normalized.columns and "date" not in normalized.columns:
            normalized = normalized.rename(columns={"datetime": "date"})
        if "index" in normalized.columns and "date" not in normalized.columns:
            normalized = normalized.rename(columns={"index": "date"})
        wanted = ["date", "open", "high", "low", "close", "volume"]
        for column in wanted:
            if column not in normalized.columns:
                normalized[column] = pd.NA
        normalized = normalized[wanted].copy()
        normalized["date"] = pd.to_datetime(normalized["date"], utc=False, errors="coerce")
        for column in ["open", "high", "low", "close", "volume"]:
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
        normalized = normalized.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
        return normalized

    @staticmethod
    def _statement_frame(frame: Any) -> pd.DataFrame:
        if frame is None:
            return pd.DataFrame()
        copied = frame.copy()
        if copied.empty:
            return copied
        if not isinstance(copied.index, pd.Index):
            return copied
        if isinstance(copied.columns, pd.Index):
            try:
                copied = copied.T
            except Exception:
                return copied
        copied.index = [str(index)[:10] for index in copied.index]
        return copied

    @staticmethod
    def _snake_case(value: str) -> str:
        text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
        text = re.sub(r"[^A-Za-z0-9]+", "_", text)
        return text.strip("_").lower()

    @staticmethod
    def _numeric(value: Any) -> float:
        if value in (None, "", pd.NA):
            return 0.0
        if isinstance(value, Decimal):
            return float(value)
        try:
            if pd.isna(value):
                return 0.0
        except Exception:
            pass
        try:
            return float(value)
        except Exception:
            return 0.0

    @staticmethod
    def _safe_ratio(numerator: float, denominator: float) -> float | None:
        if denominator in (0.0, None):
            return None
        return numerator / denominator

    @staticmethod
    def _to_float(value: Decimal | float | int | None) -> float | None:
        if value is None:
            return None
        return float(value)

    @staticmethod
    def _confidence_value(snapshot: ConfidenceSnapshot | None) -> float | None:
        if snapshot is None:
            return None
        return float(snapshot.overall_confidence)

    @staticmethod
    def _override_required(snapshot: ConfidenceSnapshot | None) -> bool:
        score = DataRouter._confidence_value(snapshot)
        return bool(score is not None and score < 40)

    @staticmethod
    def _attribution_to_dict(row: AttributionRow) -> dict[str, Any]:
        return {
            "attribution_id": row.attribution_id,
            "driver_type": row.driver_type,
            "driver_key": row.driver_key,
            "impact_value_abs": float(row.impact_value_abs),
            "impact_value_pct": float(row.impact_value_pct) if row.impact_value_pct is not None else None,
            "direction": row.direction,
            "evidence_ref": row.evidence_ref,
            "created_at": row.created_at,
        }

    @staticmethod
    def _incident_to_dict(item: IncidentSummary) -> dict[str, Any]:
        return {
            "incident_id": item.incident_id,
            "incident_status": item.incident_status,
            "impact_level": item.impact_level,
            "opened_at": item.opened_at,
            "assigned_to": item.assigned_to,
            "severity": item.severity,
            "domain": item.domain,
        }

    @staticmethod
    def _pipeline_run_to_dict(item: PipelineRun) -> dict[str, Any]:
        return {
            "pipeline_run_id": item.pipeline_run_id,
            "run_uuid": str(item.run_uuid),
            "pipeline_name": item.pipeline_name,
            "run_mode": item.run_mode,
            "triggered_by": item.triggered_by,
            "status": item.status,
            "started_at": item.started_at,
            "ended_at": item.ended_at,
        }

    @staticmethod
    def _keyword_score(text: str) -> int:
        positive = {"beat", "upgrade", "expansion", "merger", "profit", "growth", "approval"}
        negative = {"downgrade", "miss", "fraud", "decline", "probe", "delay", "loss"}
        words = {word.strip(".,:;!?()[]{}\"").lower() for word in text.split()}
        return len(words & positive) - len(words & negative)

    @staticmethod
    def _estimate_revenue_from_info(info: dict[str, Any]) -> float:
        market_cap = DataRouter._numeric(info.get("market_cap"))
        trailing_pe = DataRouter._numeric(info.get("trailing_pe"))
        profit_margin = DataRouter._numeric(info.get("profit_margins")) or 0.12
        sales_multiple = DataRouter._numeric(info.get("price_to_sales_trailing_12_months"))
        if market_cap and sales_multiple:
            return market_cap / sales_multiple
        if market_cap and trailing_pe and profit_margin:
            return market_cap / trailing_pe / profit_margin
        return max(market_cap * 0.6, 1.0)


__all__ = ["AVIS_ENABLED", "DataRouter"]
