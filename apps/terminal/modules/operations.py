"""AVIS operations module."""

from __future__ import annotations

from datetime import datetime

import altair as alt
import pandas as pd
import streamlit as st

from apps.terminal.data_router import DataRouter

STATUS_COLORS = {
    "SUCCESS": "#1f8f5f",
    "RUNNING": "#d18a00",
    "QUEUED": "#d18a00",
    "FAILED": "#c0392b",
    "ERROR": "#c0392b",
    "OVERRIDDEN": "#8e44ad",
}
STATUS_ICONS = {
    "SUCCESS": "🟢",
    "RUNNING": "🟠",
    "QUEUED": "🟠",
    "FAILED": "🔴",
    "ERROR": "🔴",
    "OVERRIDDEN": "🟣",
}


def render(router: DataRouter, selection: dict | None = None) -> None:
    st.title("🏗️ AVIS Operations")
    if not router.avis_enabled:
        st.info("AVIS operations are available only when AVIS is enabled.")
        return

    _render_pipeline_health(router)
    _render_dq_incidents(router)
    _render_coverage_map(router)
    _render_valuation_run_browser(router)


def _render_pipeline_health(router: DataRouter) -> None:
    st.subheader("Pipeline health dashboard")
    runs = router.pipeline_status() or []
    if not runs:
        st.caption("No pipeline runs available.")
        return
    latest_ten = runs[:10]
    summary_rows = []
    detail_lookup: dict[int, dict] = {}
    for run in latest_ten:
        detail = router.pipeline_run_detail(run["pipeline_run_id"]) or {}
        detail_lookup[int(run["pipeline_run_id"])] = detail
        duration = _duration_text(run.get("started_at"), run.get("ended_at"))
        status = str(run.get("status", "UNKNOWN")).upper()
        summary_rows.append(
            {
                "pipeline_run_id": run.get("pipeline_run_id"),
                "name": run.get("pipeline_name", "Unknown"),
                "status": f"{STATUS_ICONS.get(status, '⚪')} {status}",
                "start_time": run.get("started_at"),
                "duration": duration,
                "sla_breach": "YES" if detail.get("sla_breach") else "",
            }
        )
    st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)
    for run in latest_ten:
        detail = detail_lookup.get(int(run["pipeline_run_id"]), {})
        status = str(run.get("status", "UNKNOWN")).upper()
        sla_badge = " | SLA BREACH" if detail.get("sla_breach") else ""
        title = f"{STATUS_ICONS.get(status, '⚪')} {run.get('pipeline_name', 'Unknown')} | {status} | {run.get('started_at')} | {_duration_text(run.get('started_at'), run.get('ended_at'))}{sla_badge}"
        with st.expander(title):
            st.markdown(_status_badge(status), unsafe_allow_html=True)
            if detail.get("job_events"):
                events = pd.DataFrame(detail["job_events"])
                st.dataframe(events, use_container_width=True, hide_index=True)
            else:
                st.caption("No job events recorded for this run.")


def _render_dq_incidents(router: DataRouter) -> None:
    st.subheader("Data quality incidents")
    severity_filter = st.selectbox("Severity filter", ["ALL", "CRITICAL", "HIGH", "MEDIUM"], index=0)
    incidents = router.dq_incidents() or []
    if severity_filter != "ALL":
        incidents = [incident for incident in incidents if incident.get("severity") == severity_filter]
    if not incidents:
        st.caption("No open incidents match the selected filter.")
        return

    instruments = router.list_instruments() or []
    instrument_lookup = {item["instrument_uuid"]: item["symbol"] for item in instruments}
    table_rows = []
    for incident in incidents:
        instrument_symbol = _instrument_from_key(incident.get("target_record_key"), instrument_lookup)
        table_rows.append(
            {
                "incident_id": incident.get("incident_id"),
                "severity": incident.get("severity"),
                "domain": incident.get("domain"),
                "instrument": instrument_symbol,
                "created_at": incident.get("opened_at"),
            }
        )
    st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)

    analyst_id_configured = bool(__import__("os").getenv("AVIS_ANALYST_ID"))
    if not analyst_id_configured:
        st.caption("Set `AVIS_ANALYST_ID` to enable incident overrides from the terminal.")

    for incident in incidents:
        instrument_symbol = _instrument_from_key(incident.get("target_record_key"), instrument_lookup)
        expander_title = f"Incident {incident['incident_id']} | {incident.get('severity')} | {incident.get('domain')} | {instrument_symbol}"
        with st.expander(expander_title):
            detail_frame = pd.DataFrame(
                [
                    {
                        "incident_id": incident.get("incident_id"),
                        "status": incident.get("incident_status"),
                        "impact_level": incident.get("impact_level"),
                        "domain": incident.get("domain"),
                        "severity": incident.get("severity"),
                        "target_table": incident.get("target_table"),
                        "target_record_key": incident.get("target_record_key"),
                        "failure_reason": incident.get("failure_reason"),
                        "opened_at": incident.get("opened_at"),
                    }
                ]
            )
            st.dataframe(detail_frame, use_container_width=True, hide_index=True)
            with st.form(f"override_{incident['incident_id']}"):
                justification = st.text_area("Justification", key=f"justification_{incident['incident_id']}")
                submitted = st.form_submit_button("Override")
                if submitted:
                    if len(justification.strip()) < 20:
                        st.error("Justification must be at least 20 characters.")
                    else:
                        result = router.resolve_incident(incident["incident_id"], justification)
                        if result is None:
                            st.error("Unable to resolve incident. Check AVIS availability and analyst configuration.")
                        else:
                            st.success(f"Incident {result['incident_id']} resolved with override {result['override_id']}")


def _render_coverage_map(router: DataRouter) -> None:
    st.subheader("Coverage map")
    exchange = st.selectbox("Exchange filter", ["ALL", "NSE", "BSE"], index=0, key="coverage_exchange")
    coverage = router.coverage_map(exchange=None if exchange == "ALL" else exchange)
    if coverage is None or coverage.empty:
        st.caption("No security master coverage available.")
        return
    coverage = coverage.copy()
    coverage["last_price_date"] = pd.to_datetime(coverage["last_price_date"], errors="coerce")
    coverage["last_val_date"] = pd.to_datetime(coverage["last_val_date"], errors="coerce")
    coverage["valuation_status"] = coverage["valued"].map({True: "Valued", False: "Not yet valued"})
    st.dataframe(
        coverage[["symbol", "isin", "exchange", "last_price_date", "last_val_date", "valuation_status"]],
        use_container_width=True,
        hide_index=True,
    )


def _render_valuation_run_browser(router: DataRouter) -> None:
    st.subheader("Valuation run browser")
    runs = router.valuation_run_browser()
    if runs is None or runs.empty:
        st.caption("No AVIS valuation runs available.")
        return
    st.dataframe(
        runs[["instrument", "run_date", "blended_value", "confidence_score", "override_required", "status"]],
        use_container_width=True,
        hide_index=True,
    )
    for row in runs.head(50).to_dict(orient="records"):
        title = f"Run {row['val_run_id']} | {row['instrument']} | {row['run_date']} | {row['status']}"
        with st.expander(title):
            detail = router.valuation_run_detail(int(row["val_run_id"]))
            if not detail:
                st.caption("Unable to load run detail.")
                continue
            attribution = pd.DataFrame(detail.get("attribution_rows", []))
            cols = st.columns(4)
            with cols[0]:
                st.metric("Blended Value", _format_number(detail.get("blended_value")))
            with cols[1]:
                st.metric("DCF Value", _format_number(detail.get("dcf_value")))
            with cols[2]:
                st.metric("Confidence", _format_number(detail.get("confidence_score")))
            with cols[3]:
                st.metric("Override Required", "Yes" if detail.get("override_required") else "No")
            if attribution.empty:
                st.caption("No attribution rows available.")
                continue
            chart_frame = attribution.copy()
            chart_frame["impact_value_abs"] = pd.to_numeric(chart_frame["impact_value_abs"], errors="coerce")
            chart = alt.Chart(chart_frame).mark_bar().encode(
                x=alt.X("driver_key:N", sort=None, title="Driver"),
                y=alt.Y("impact_value_abs:Q", title="Impact Value"),
                color=alt.condition(alt.datum.impact_value_abs >= 0, alt.value("#1f8f5f"), alt.value("#c0392b")),
                tooltip=["driver_type", "driver_key", "impact_value_abs", "impact_value_pct"],
            )
            st.altair_chart(chart, use_container_width=True)
            st.dataframe(attribution, use_container_width=True, hide_index=True)


def _status_badge(status: str) -> str:
    color = STATUS_COLORS.get(status.upper(), "#54616f")
    return f"<span style='color:{color};font-weight:700;'>{status}</span>"


def _duration_text(started_at, ended_at) -> str:
    if started_at is None:
        return "duration unavailable"
    start = pd.to_datetime(started_at)
    end = pd.to_datetime(ended_at) if ended_at is not None else pd.Timestamp(datetime.utcnow())
    duration = end - start
    total_seconds = int(duration.total_seconds())
    if total_seconds < 60:
        return f"{total_seconds}s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _instrument_from_key(record_key: str | None, instrument_lookup: dict[str, str]) -> str:
    if not record_key:
        return "Unknown"
    for instrument_uuid, symbol in instrument_lookup.items():
        if instrument_uuid in record_key:
            return symbol
    return record_key


def _format_number(value) -> str:
    if value is None or value == "":
        return "N/A"
    try:
        return f"{float(value):,.2f}"
    except Exception:
        return str(value)
