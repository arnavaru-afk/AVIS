"""Technical Analysis module."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from apps.terminal.data_router import DataRouter


def render(router: DataRouter, selection: dict | None) -> None:
    st.title("Technical Analysis")
    if not selection:
        st.info("Search for a ticker in the sidebar to run technical analysis.")
        return
    symbol = selection["symbol"]
    instrument_uuid = selection.get("instrument_uuid")
    period = st.selectbox("Period", ["6mo", "1y", "2y"], index=1)
    history = router.price_history(symbol, instrument_uuid=instrument_uuid, period=period)
    if history.empty:
        st.warning("Price history unavailable.")
        return

    history = history.copy()
    history["rsi"] = _rsi(history["close"])
    macd_frame = _macd(history["close"])
    history = pd.concat([history, macd_frame], axis=1)
    bands = _bollinger(history["close"])
    history = pd.concat([history, bands], axis=1)
    history["dma_50"] = history["close"].rolling(50).mean()
    history["dma_200"] = history["close"].rolling(200).mean()

    st.subheader("Price and moving averages")
    st.line_chart(history.set_index("date")[["close", "dma_50", "dma_200"]])

    cols = st.columns(2)
    with cols[0]:
        st.subheader("RSI")
        st.line_chart(history.set_index("date")[["rsi"]])
    with cols[1]:
        st.subheader("MACD")
        st.line_chart(history.set_index("date")[["macd", "signal"]])

    st.subheader("Bollinger Bands")
    st.line_chart(history.set_index("date")[["close", "upper_band", "lower_band"]])

    st.subheader("Indicator table")
    st.dataframe(history.tail(20), use_container_width=True, hide_index=True)


def _rsi(series: pd.Series, window: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(window).mean()
    loss = -delta.clip(upper=0).rolling(window).mean()
    rs = gain / loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def _macd(series: pd.Series) -> pd.DataFrame:
    ema_12 = series.ewm(span=12, adjust=False).mean()
    ema_26 = series.ewm(span=26, adjust=False).mean()
    macd = ema_12 - ema_26
    signal = macd.ewm(span=9, adjust=False).mean()
    return pd.DataFrame({"macd": macd, "signal": signal})


def _bollinger(series: pd.Series, window: int = 20) -> pd.DataFrame:
    mean = series.rolling(window).mean()
    std = series.rolling(window).std()
    return pd.DataFrame({"upper_band": mean + 2 * std, "lower_band": mean - 2 * std})
