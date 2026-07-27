"""Fundamental Deep Dive module."""

from __future__ import annotations

from io import StringIO
import html

import pandas as pd
import streamlit as st

from apps.terminal.components.metric_card import metric_card
from apps.terminal.data_router import DataRouter


def render(router: DataRouter, selection: dict | None) -> None:
    st.title("Fundamental Deep Dive")
    if not selection:
        st.info("Search for a ticker in the sidebar to start the deep dive.")
        return

    symbol = selection["symbol"]
    instrument_uuid = selection.get("instrument_uuid")
    info = router.instrument_info(symbol, instrument_uuid)
    statements = router.financial_statements(symbol)
    price_history = router.price_history(symbol, instrument_uuid=instrument_uuid, period="1y")
    news = router.company_news(info.get("company_name") or symbol, limit=12)
    analyst_actions = router.analyst_actions(symbol)

    _render_corporate_overview(info)
    _render_institutional_telemetry(price_history)
    _render_nlp_catalyst_feed(news)
    _render_analyst_accountability(analyst_actions)

    ratios = _calculate_quality_ratios(info, statements)
    _render_liquidity_quality(ratios)
    _render_working_capital_efficiency(ratios)
    _render_margin_trends(statements)
    _render_ratio_interpreter(ratios)
    _render_latest_news(news)
    _render_avis_data_quality(router, instrument_uuid)
    _render_exports(info, ratios, news)


def _render_corporate_overview(info: dict) -> None:
    st.subheader("Corporate overview")
    cols = st.columns(3)
    with cols[0]:
        metric_card("Company", str(info.get("company_name") or info.get("long_name") or info.get("symbol") or "N/A"))
    with cols[1]:
        metric_card("Sector", str(info.get("sector") or "N/A"))
    with cols[2]:
        metric_card("Exchange", str(info.get("exchange") or info.get("full_exchange_name") or "N/A"))
    summary = info.get("long_business_summary") or info.get("summary") or "No company summary was available from the current data source."
    st.write(summary)


def _render_institutional_telemetry(price_history: pd.DataFrame) -> None:
    st.subheader("Institutional telemetry")
    if price_history.empty:
        st.caption("Price history unavailable.")
        return
    frame = price_history.copy()
    frame["return"] = frame["close"].pct_change()
    alpha = frame["return"].mean() * 252
    volume_footprint = frame["volume"].tail(20).mean() / max(frame["volume"].mean(), 1)
    cols = st.columns(2)
    with cols[0]:
        metric_card("Alpha", f"{alpha:.2%}", "Annualized trailing return drift")
    with cols[1]:
        metric_card("Volume Footprint", f"{volume_footprint:.2f}x", "20-day volume vs trailing mean")


def _render_nlp_catalyst_feed(news: list[dict]) -> None:
    st.subheader("NLP catalyst feed")
    if not news:
        st.caption("No news items available.")
        return
    frame = pd.DataFrame(news)[["title", "source", "published", "keyword_score"]]
    st.dataframe(frame, use_container_width=True, hide_index=True)


def _render_analyst_accountability(actions: pd.DataFrame) -> None:
    st.subheader("Analyst accountability wall")
    if actions.empty:
        st.caption("No upgrades or downgrades were available from the current data source.")
        return
    st.dataframe(actions.head(25), use_container_width=True, hide_index=True)


def _calculate_quality_ratios(info: dict, statements: dict[str, pd.DataFrame]) -> dict[str, float | None]:
    balance = statements.get("balance_sheet", pd.DataFrame())
    income = statements.get("income_statement", pd.DataFrame())
    cashflow = statements.get("cashflow", pd.DataFrame())
    current_assets = _statement_value(balance, "Current Assets")
    current_liabilities = _statement_value(balance, "Current Liabilities")
    inventory = _statement_value(balance, "Inventory")
    revenue = _statement_value(income, "Total Revenue") or _statement_value(income, "Revenue")
    cogs = abs(_statement_value(income, "Cost Of Revenue"))
    receivables = _statement_value(balance, "Accounts Receivable")
    payables = _statement_value(balance, "Accounts Payable")
    fcff = _statement_value(cashflow, "Free Cash Flow") or _statement_value(cashflow, "Operating Cash Flow") - abs(_statement_value(cashflow, "Capital Expenditure"))
    cfo = _statement_value(cashflow, "Operating Cash Flow")
    net_income = _statement_value(income, "Net Income")
    gross_profit = _statement_value(income, "Gross Profit")
    ebitda = _statement_value(income, "EBITDA") or float(info.get("ebitda") or 0.0)
    equity = _statement_value(balance, "Stockholders Equity") or _statement_value(balance, "Total Equity Gross Minority Interest")
    return {
        "current_ratio": _safe_ratio(current_assets, current_liabilities),
        "quick_ratio": _safe_ratio(current_assets - inventory, current_liabilities),
        "fcff": fcff,
        "cfo_net_income": _safe_ratio(cfo, net_income),
        "dso": _safe_ratio(receivables * 365, revenue),
        "doh": _safe_ratio(inventory * 365, cogs),
        "dpo": _safe_ratio(payables * 365, cogs),
        "gross_margin": _safe_ratio(gross_profit, revenue),
        "ebitda_margin": _safe_ratio(ebitda, revenue),
        "net_margin": _safe_ratio(net_income, revenue),
        "roe": _safe_ratio(net_income, equity),
    }


def _render_liquidity_quality(ratios: dict[str, float | None]) -> None:
    st.subheader("Liquidity and earnings quality")
    cols = st.columns(4)
    values = [
        ("Current Ratio", ratios.get("current_ratio")),
        ("Quick Ratio", ratios.get("quick_ratio")),
        ("FCFF", ratios.get("fcff")),
        ("CFO / Net Income", ratios.get("cfo_net_income")),
    ]
    for column, (label, value) in zip(cols, values, strict=False):
        with column:
            metric_card(label, _format_metric(value))


def _render_working_capital_efficiency(ratios: dict[str, float | None]) -> None:
    st.subheader("Working capital efficiency")
    dso = ratios.get("dso")
    doh = ratios.get("doh")
    dpo = ratios.get("dpo")
    ccc = None
    if None not in (dso, doh, dpo):
        ccc = dso + doh - dpo
    cols = st.columns(4)
    for column, (label, value) in zip(cols, [("DSO", dso), ("DOH", doh), ("DPO", dpo), ("CCC", ccc)], strict=False):
        with column:
            metric_card(label, _format_metric(value))


def _render_margin_trends(statements: dict[str, pd.DataFrame]) -> None:
    st.subheader("Historical margin trends")
    income = statements.get("income_statement", pd.DataFrame())
    balance = statements.get("balance_sheet", pd.DataFrame())
    if income.empty:
        st.caption("Income statement history unavailable.")
        return
    rows = []
    for period in income.index[:8]:
        revenue = _row_value(income, period, ["Total Revenue", "Revenue"])
        gross_profit = _row_value(income, period, ["Gross Profit"])
        ebitda = _row_value(income, period, ["EBITDA"])
        net_income = _row_value(income, period, ["Net Income"])
        equity = _row_value(balance, period, ["Stockholders Equity", "Total Equity Gross Minority Interest"])
        rows.append(
            {
                "period": period,
                "gross_margin": _safe_ratio(gross_profit, revenue),
                "ebitda_margin": _safe_ratio(ebitda, revenue),
                "net_margin": _safe_ratio(net_income, revenue),
                "roe": _safe_ratio(net_income, equity),
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        st.caption("Margin history unavailable.")
        return
    st.line_chart(frame.set_index("period"))


def _render_ratio_interpreter(ratios: dict[str, float | None]) -> None:
    st.subheader("Ratio interpreter")
    cards = []
    current_ratio = ratios.get("current_ratio")
    if current_ratio is not None:
        cards.append(("Liquidity", "Comfortable" if current_ratio >= 1.5 else "Watch list", f"Current ratio at {_format_metric(current_ratio)}"))
    cfo_net_income = ratios.get("cfo_net_income")
    if cfo_net_income is not None:
        cards.append(("Earnings Quality", "Healthy" if cfo_net_income >= 1 else "Fragile", f"CFO / Net Income at {_format_metric(cfo_net_income)}"))
    roe = ratios.get("roe")
    if roe is not None:
        cards.append(("Capital Efficiency", "Strong" if roe >= 0.15 else "Average", f"ROE at {_format_metric(roe)}"))
    if not cards:
        st.caption("Not enough data to interpret ratios.")
        return
    cols = st.columns(len(cards))
    for column, (title, status, detail) in zip(cols, cards, strict=False):
        with column:
            metric_card(title, status, detail)


def _render_latest_news(news: list[dict]) -> None:
    st.subheader("Latest news feed")
    if not news:
        st.caption("No current news items found.")
        return
    for item in news[:6]:
        st.markdown(f"- [{item['title']}]({item['link']})")
        st.caption(f"{item['source']} | {item['published']}")


def _render_avis_data_quality(router: DataRouter, instrument_uuid: str | None) -> None:
    if not instrument_uuid:
        return
    history = router.valuation_history(instrument_uuid)
    incidents = router.dq_incidents()
    pipeline = router.pipeline_status()
    if history is None and incidents is None and pipeline is None:
        return
    st.subheader("AVIS Data Quality")
    latest_confidence = history[0]["confidence_score"] if history else None
    open_incidents = 0
    if incidents:
        open_incidents = sum(
            1
            for incident in incidents
            if incident.get("incident_status") == "OPEN"
            and instrument_uuid in str(incident.get("target_record_key") or "")
        )
    last_ingestion = pipeline[0].get("ended_at") if pipeline else None
    cols = st.columns(3)
    with cols[0]:
        metric_card("Confidence Score", _format_metric(latest_confidence))
    with cols[1]:
        metric_card("Open DQ Incidents", str(open_incidents))
    with cols[2]:
        metric_card("Last Ingestion", str(last_ingestion) if last_ingestion else "N/A")


def _render_exports(info: dict, ratios: dict[str, float | None], news: list[dict]) -> None:
    st.subheader("Export")
    summary_frame = pd.DataFrame(
        {
            "metric": list(ratios.keys()),
            "value": [ratios[key] for key in ratios],
        }
    )
    st.download_button("Download CSV", summary_frame.to_csv(index=False), file_name="fundamental_deep_dive.csv", mime="text/csv")
    html_buffer = StringIO()
    html_buffer.write("<html><body>")
    html_buffer.write(f"<h1>{html.escape(str(info.get('company_name') or info.get('symbol') or 'Company'))}</h1>")
    html_buffer.write("<h2>Key Ratios</h2><ul>")
    for key, value in ratios.items():
        html_buffer.write(f"<li>{html.escape(key)}: {html.escape(_format_metric(value))}</li>")
    html_buffer.write("</ul><h2>News</h2><ul>")
    for item in news[:10]:
        html_buffer.write(f"<li><a href='{html.escape(item['link'])}'>{html.escape(item['title'])}</a></li>")
    html_buffer.write("</ul></body></html>")
    st.download_button("Download HTML report", html_buffer.getvalue(), file_name="fundamental_deep_dive.html", mime="text/html")


def _statement_value(frame: pd.DataFrame, label: str) -> float:
    if frame.empty or label not in frame.columns:
        return 0.0
    series = pd.to_numeric(frame[label], errors="coerce")
    return float(series.dropna().iloc[0]) if not series.dropna().empty else 0.0


def _row_value(frame: pd.DataFrame, period: str, labels: list[str]) -> float:
    if frame.empty or period not in frame.index:
        return 0.0
    for label in labels:
        if label in frame.columns:
            value = pd.to_numeric(pd.Series([frame.loc[period, label]]), errors="coerce").iloc[0]
            return float(value) if pd.notna(value) else 0.0
    return 0.0


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    if denominator in (0.0, None):
        return None
    return numerator / denominator


def _format_metric(value: float | None) -> str:
    if value is None:
        return "N/A"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 1:
        return f"{value:,.2f}"
    return f"{value:.2%}"
