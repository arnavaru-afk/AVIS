"""Valuation Modeler module."""

from __future__ import annotations

from io import BytesIO

import altair as alt
import pandas as pd
import streamlit as st
from openpyxl import Workbook

from apps.terminal.components.metric_card import metric_card
from apps.terminal.data_router import AVIS_ENABLED, DataRouter


def render(router: DataRouter, selection: dict | None) -> None:
    st.title("Valuation Modeler")
    if not selection:
        st.info("Search for a ticker in the sidebar to start the valuation workflow.")
        return

    symbol = selection["symbol"]
    instrument_uuid = selection.get("instrument_uuid")
    info = router.instrument_info(symbol, instrument_uuid)
    statements = router.financial_statements(symbol)

    st.subheader("Live valuation multiples")
    multiples = _live_multiples(info)
    cols = st.columns(4)
    for column, (label, value) in zip(cols, multiples.items(), strict=False):
        with column:
            metric_card(label, _display_number(value))

    st.subheader("Peer comparables table with auto-detection")
    peer_input = st.text_input("Peers", value=", ".join(_auto_detect_peers(symbol, info)), help="Comma-separated symbols")
    peers = [item.strip() for item in peer_input.split(",") if item.strip()]
    peer_frame = router.peer_multiples(peers)
    if peer_frame.empty:
        st.caption("Peer data unavailable.")
    else:
        st.dataframe(peer_frame, use_container_width=True, hide_index=True)

    st.subheader("Historical P/E normalization bands")
    price_history = router.price_history(symbol, instrument_uuid=instrument_uuid, period="3y")
    pe_history = _historical_pe_series(price_history, info)
    if pe_history.empty:
        st.caption("Historical P/E normalization unavailable.")
    else:
        st.line_chart(pe_history.set_index("date"))

    st.subheader("DCF engine")
    c1, c2, c3 = st.columns(3)
    with c1:
        wacc = st.slider("WACC", min_value=0.05, max_value=0.20, value=0.10, step=0.005)
        revenue_cagr = st.slider("FCF growth proxy", min_value=-0.05, max_value=0.25, value=0.08, step=0.01)
    with c2:
        terminal_growth = st.slider("Terminal growth", min_value=0.00, max_value=0.08, value=0.04, step=0.005)
        ebit_margin = st.slider("EBIT margin", min_value=0.05, max_value=0.50, value=0.18, step=0.01)
    with c3:
        forecast_years = st.slider("Forecast years", min_value=3, max_value=10, value=5, step=1)
        use_avis = st.toggle("Use AVIS valuation engine", value=bool(AVIS_ENABLED and instrument_uuid))

    local_result = router.local_dcf_valuation(
        symbol=symbol,
        wacc=wacc,
        terminal_growth=terminal_growth,
        forecast_years=forecast_years,
        revenue_cagr=revenue_cagr,
        ebit_margin=ebit_margin,
    )
    avis_result = None
    if use_avis and instrument_uuid:
        avis_result = router.run_dcf_valuation(
            instrument_uuid,
            wacc=wacc,
            terminal_growth=terminal_growth,
            forecast_years=forecast_years,
            revenue_cagr=revenue_cagr,
            ebit_margin=ebit_margin,
            symbol=symbol,
        )
    result_to_display = avis_result or local_result

    if avis_result and avis_result.get("override_required"):
        st.warning("Low confidence — analyst review recommended")

    cols = st.columns(3)
    avis_dcf = avis_result.get("dcf_value") if avis_result else None
    local_dcf = local_result.get("dcf_value")
    delta = None
    if avis_dcf not in (None, 0) and local_dcf is not None:
        delta = (avis_dcf - local_dcf) / avis_dcf
    for column, (label, value) in zip(cols, [("AVIS DCF", avis_dcf), ("Local DCF", local_dcf), ("Delta %", delta)], strict=False):
        with column:
            metric_card(label, _display_number(value, percent=(label == "Delta %")))

    st.subheader("Sensitivity analysis heat map")
    sensitivity = _sensitivity_grid(router, symbol, wacc, terminal_growth, forecast_years, revenue_cagr, ebit_margin)
    if not sensitivity.empty:
        chart = alt.Chart(sensitivity).mark_rect().encode(
            x=alt.X("terminal_growth:O", title="Terminal Growth"),
            y=alt.Y("wacc:O", title="WACC"),
            color=alt.Color("intrinsic_value:Q", title="Intrinsic Value"),
            tooltip=["wacc", "terminal_growth", "intrinsic_value"],
        )
        st.altair_chart(chart, use_container_width=True)

    st.subheader("Attribution table")
    attribution_rows = pd.DataFrame(result_to_display.get("attribution_rows", []))
    if attribution_rows.empty:
        st.caption("Attribution data unavailable.")
    else:
        st.dataframe(attribution_rows, use_container_width=True, hide_index=True)

    st.subheader("Valuation history chart")
    history = router.valuation_history(instrument_uuid) if instrument_uuid else None
    history_frame = pd.DataFrame(history or [])
    if history_frame.empty:
        st.caption("No historical AVIS valuations available.")
    else:
        history_frame["as_of_ts"] = pd.to_datetime(history_frame["as_of_ts"])
        chart = alt.Chart(history_frame).mark_line(point=True).encode(
            x="as_of_ts:T",
            y=alt.Y("blended_value:Q", title="Intrinsic Value"),
            tooltip=["run_label", "status", "blended_value", "confidence_score"],
        )
        st.altair_chart(chart, use_container_width=True)

    st.subheader("Excel financial model download")
    workbook_bytes = _build_workbook(info, local_result, avis_result, statements)
    st.download_button(
        "Download Excel model",
        workbook_bytes,
        file_name=f"{symbol.replace('.', '_')}_valuation_model.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    st.subheader("3-year financial statements preview")
    for label, frame in statements.items():
        st.markdown(f"**{label.replace('_', ' ').title()}**")
        if frame.empty:
            st.caption("Unavailable")
        else:
            st.dataframe(frame.head(3), use_container_width=True)


def _live_multiples(info: dict) -> dict[str, float | None]:
    market_cap = _number(info.get("market_cap"))
    enterprise_value = _number(info.get("enterprise_value"))
    ebitda = _number(info.get("ebitda"))
    price = _number(info.get("current_price") or info.get("regular_market_price"))
    trailing_eps = _number(info.get("trailing_eps"))
    book_value = _number(info.get("book_value"))
    sales = _number(info.get("total_revenue"))
    return {
        "P/E": _ratio(price, trailing_eps),
        "EV/EBITDA": _ratio(enterprise_value, ebitda),
        "P/B": _ratio(price, book_value),
        "P/S": _ratio(market_cap, sales),
    }


def _auto_detect_peers(symbol: str, info: dict) -> list[str]:
    exchange = str(info.get("exchange") or info.get("full_exchange_name") or "")
    suffix = ".NS" if "NSE" in exchange.upper() else ".BO" if "BSE" in exchange.upper() else ""
    base = symbol.split(".")[0]
    candidates = [symbol]
    if suffix and not symbol.endswith(suffix):
        candidates.append(f"{base}{suffix}")
    return candidates[:3]


def _historical_pe_series(price_history: pd.DataFrame, info: dict) -> pd.DataFrame:
    if price_history.empty:
        return pd.DataFrame()
    trailing_eps = _number(info.get("trailing_eps"))
    if trailing_eps == 0:
        return pd.DataFrame()
    frame = price_history[["date", "close"]].copy()
    frame["trailing_pe"] = frame["close"] / trailing_eps
    return frame[["date", "trailing_pe"]]


def _sensitivity_grid(router: DataRouter, symbol: str, wacc: float, terminal_growth: float, forecast_years: int, revenue_cagr: float, ebit_margin: float) -> pd.DataFrame:
    rows = []
    for wacc_shift in (-0.01, 0.0, 0.01):
        for growth_shift in (-0.01, 0.0, 0.01):
            result = router.local_dcf_valuation(
                symbol=symbol,
                wacc=max(wacc + wacc_shift, 0.01),
                terminal_growth=max(terminal_growth + growth_shift, 0.0),
                forecast_years=forecast_years,
                revenue_cagr=revenue_cagr,
                ebit_margin=ebit_margin,
            )
            rows.append(
                {
                    "wacc": f"{max(wacc + wacc_shift, 0.01):.1%}",
                    "terminal_growth": f"{max(terminal_growth + growth_shift, 0.0):.1%}",
                    "intrinsic_value": result["dcf_value"],
                }
            )
    return pd.DataFrame(rows)


def _build_workbook(info: dict, local_result: dict, avis_result: dict | None, statements: dict[str, pd.DataFrame]) -> bytes:
    workbook = Workbook()
    summary = workbook.active
    summary.title = "Summary"
    summary.append(["Company", info.get("company_name") or info.get("long_name") or info.get("symbol")])
    summary.append(["Local DCF", local_result.get("dcf_value")])
    summary.append(["AVIS DCF", avis_result.get("dcf_value") if avis_result else None])
    for name, frame in statements.items():
        sheet = workbook.create_sheet(name[:31])
        if frame.empty:
            sheet.append(["No data"])
            continue
        sheet.append(["period", *frame.columns.tolist()])
        for idx, row in frame.head(5).iterrows():
            sheet.append([idx, *row.tolist()])
    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer.getvalue()


def _ratio(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def _number(value) -> float:
    try:
        if value is None or pd.isna(value):
            return 0.0
    except Exception:
        if value is None:
            return 0.0
    try:
        return float(value)
    except Exception:
        return 0.0


def _display_number(value: float | None, *, percent: bool = False) -> str:
    if value is None:
        return "N/A"
    if percent:
        return f"{value:.2%}"
    return f"{value:,.2f}"
