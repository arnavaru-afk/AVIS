"""Sidebar ticker search widget."""

from __future__ import annotations

import streamlit as st

from apps.terminal.data_router import DataRouter


def ticker_search(router: DataRouter) -> dict | None:
    st.sidebar.subheader("Instrument")
    query = st.sidebar.text_input("Ticker or company", value=st.session_state.get("terminal_query", ""), key="terminal_query")
    if not query.strip():
        return None
    selection = router.resolve_ticker(query)
    if selection:
        st.sidebar.caption(f"{selection.get('name', selection.get('symbol', ''))} | {selection.get('exchange', 'Unknown exchange')}")
        st.session_state["terminal_selection"] = selection
        return selection
    return st.session_state.get("terminal_selection")
