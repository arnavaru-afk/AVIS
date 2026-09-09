"""Valuation client wrapper for AVIS API."""

from __future__ import annotations

from decimal import Decimal

from avis.client.base import AVISClient
from avis.client.models import (
    AttributionDetail,
    AttributionRow,
    ValuationDetail,
    ValuationJob,
    ValuationListResponse,
    ValuationSummary,
)


class ValuationsClient(AVISClient):
    def run_valuation(
        self,
        instrument_uuid: str,
        wacc: Decimal | str | float,
        terminal_growth: Decimal | str | float,
        forecast_years: int,
        revenue_cagr: Decimal | str | float,
        ebit_margin: Decimal | str | float,
    ) -> ValuationJob | None:
        payload = self._post(
            "/api/v1/valuations/run",
            json_body={
                "instrument_uuid": instrument_uuid,
                "run_label": "streamlit-client",
                "assumption_set": {
                    "wacc": str(wacc),
                    "terminal_growth": str(terminal_growth),
                    "forecast_years": forecast_years,
                    "revenue_cagr": str(revenue_cagr),
                    "ebit_margin": str(ebit_margin),
                },
            },
        )
        if payload is None:
            return None
        return self._parse_dataclass(payload, ValuationJob)

    def get_valuation(self, val_run_id: int) -> ValuationDetail | None:
        payload = self._get(f"/api/v1/valuations/{val_run_id}")
        if payload is None:
            return None
        return self._parse_dataclass(payload, ValuationDetail)

    def list_valuations(self, instrument_uuid: str) -> list[ValuationSummary] | None:
        if not self.enabled:
            return None
        page = 1
        items: list[ValuationSummary] = []
        total = None
        while total is None or len(items) < total:
            payload = self._get(
                f"/api/v1/valuations/instrument/{instrument_uuid}",
                params={"page": page, "page_size": 200},
            )
            parsed = self._parse_dataclass(payload, ValuationListResponse)
            items.extend(parsed.items)
            total = parsed.pagination.total
            if (page * parsed.pagination.page_size) >= parsed.pagination.total:
                break
            page += 1
        return items

    def get_attribution(self, val_run_id: int) -> list[AttributionRow] | None:
        payload = self._get(f"/api/v1/valuations/{val_run_id}/attribution")
        if payload is None:
            return None
        parsed = self._parse_dataclass(payload, AttributionDetail)
        return parsed.rows
