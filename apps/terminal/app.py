"""Streamlit entrypoint for the Global Market Terminal."""

from __future__ import annotations

import streamlit as st

from apps.terminal.components.avis_status import render_avis_status
from apps.terminal.components.ticker_search import ticker_search
from apps.terminal.data_router import DataRouter
from apps.terminal.modules import fundamental, macro_capm, operations, technical, valuation


def main() -> None:
    st.set_page_config(page_title="Global Market Terminal", layout="wide")
    router = DataRouter()
    modules = {
        "Fundamental Deep Dive": fundamental.render,
        "Valuation Modeler": valuation.render,
        "Technical Analysis": technical.render,
        "Macro & CAPM": macro_capm.render,
    }
    if router.avis_enabled:
        modules["🏗️ AVIS Operations"] = operations.render
    st.sidebar.title("Global Market Terminal")
    render_avis_status(router)
    selection = ticker_search(router)
    module_name = st.sidebar.radio("Module", list(modules.keys()))
    modules[module_name](router, selection)


if __name__ == "__main__":
    main()
