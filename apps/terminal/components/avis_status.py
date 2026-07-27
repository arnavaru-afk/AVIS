"""Sidebar AVIS connection status badge."""

from __future__ import annotations

import streamlit as st

from apps.terminal.data_router import DataRouter


def render_avis_status(router: DataRouter) -> None:
    status = router.avis_status()
    state = status.get("status")
    if state == "connected":
        badge = "🟢 AVIS connected"
        detail = f"{status.get('instrument_count', 0)} instruments | last pipeline {status.get('last_pipeline_run')}"
    elif state == "degraded":
        badge = "🟡 AVIS degraded"
        detail = f"{status.get('instrument_count', 0)} instruments | last pipeline {status.get('last_pipeline_run')}"
    else:
        badge = "🔴 AVIS offline"
        detail = "falling back to yfinance for all data"
    st.sidebar.markdown(
        f"<div style='padding:0.8rem;border-radius:12px;background:#eef4f8;border:1px solid #d5e0e7;'>"
        f"<div style='font-weight:700;color:#12202b;'>{badge}</div>"
        f"<div style='font-size:0.82rem;color:#556473;margin-top:0.25rem;'>{detail}</div>"
        "</div>",
        unsafe_allow_html=True,
    )
