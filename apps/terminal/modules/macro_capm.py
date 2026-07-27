"""Macro & CAPM module."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from apps.terminal.components.metric_card import metric_card
from apps.terminal.data_router import DataRouter

MACRO_TICKERS = {
    "S&P 500": "^GSPC",
    "Nifty 50": "^NSEI",
    "US 10Y": "^TNX",
    "Brent": "BZ=F",
    "Gold": "GC=F",
    "Dollar Index": "DX-Y.NYB",
}


def render(router: DataRouter, selection: dict | None) -> None:
    st.title("Macro & CAPM")
    st.subheader("Macro dashboard")
    macro_series = router.macro_series(MACRO_TICKERS, period="1y")
    cols = st.columns(3)
    metrics = []
    for label, frame in macro_series.items():
        if frame.empty:
            metrics.append((label, None))
            continue
        start = frame["close"].iloc[0]
        end = frame["close"].iloc[-1]
        change = (end / start - 1) if start else None
        metrics.append((label, change))
    for column, (label, change) in zip(cols * 2, metrics, strict=False):
        with column:
            metric_card(label, _format_percent(change))
    combined = _combine_macro_series(macro_series)
    if not combined.empty:
        st.line_chart(combined)

    st.subheader("CAPM")
    if not selection:
        st.info("Search for a ticker in the sidebar to run CAPM on an instrument.")
        return
    symbol = selection["symbol"]
    instrument_uuid = selection.get("instrument_uuid")
    benchmark = st.selectbox("Benchmark", ["^GSPC", "^NSEI"], index=1 if symbol.endswith(".NS") or symbol.endswith(".BO") else 0)
    risk_free_rate = st.slider("Risk-free rate", min_value=0.00, max_value=0.15, value=0.07, step=0.005)
    expected_market_return = st.slider("Expected market return", min_value=0.00, max_value=0.20, value=0.12, step=0.005)

    asset = router.price_history(symbol, instrument_uuid=instrument_uuid, period="1y")
    market = router.price_history(benchmark, period="1y")
    beta, alpha, expected_return = _capm(asset, market, risk_free_rate, expected_market_return)
    cols = st.columns(3)
    for column, (label, value) in zip(cols, [("Beta", beta), ("Alpha", alpha), ("Expected Return", expected_return)], strict=False):
        with column:
            metric_card(label, _format_percent(value) if label != "Beta" else _format_number(value))


def _combine_macro_series(series: dict[str, pd.DataFrame]) -> pd.DataFrame:
    merged = pd.DataFrame()
    for label, frame in series.items():
        if frame.empty:
            continue
        subset = frame[["date", "close"]].rename(columns={"close": label}).set_index("date")
        merged = subset if merged.empty else merged.join(subset, how="outer")
    return merged.sort_index()


def _capm(asset: pd.DataFrame, market: pd.DataFrame, risk_free_rate: float, expected_market_return: float) -> tuple[float | None, float | None, float | None]:
    if asset.empty or market.empty:
        return None, None, None
    merged = asset[["date", "close"]].rename(columns={"close": "asset"}).merge(
        market[["date", "close"]].rename(columns={"close": "market"}),
        on="date",
        how="inner",
    )
    if merged.empty:
        return None, None, None
    merged["asset_ret"] = merged["asset"].pct_change()
    merged["market_ret"] = merged["market"].pct_change()
    merged = merged.dropna()
    if merged.empty:
        return None, None, None
    covariance = merged["asset_ret"].cov(merged["market_ret"])
    variance = merged["market_ret"].var()
    beta = covariance / variance if variance else None
    alpha = merged["asset_ret"].mean() - (risk_free_rate / 252) - (beta * (merged["market_ret"].mean() - risk_free_rate / 252) if beta is not None else 0)
    expected_return = risk_free_rate + (beta * (expected_market_return - risk_free_rate) if beta is not None else 0)
    return beta, alpha * 252 if alpha is not None else None, expected_return


def _format_percent(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:.2%}"


def _format_number(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:.2f}"
