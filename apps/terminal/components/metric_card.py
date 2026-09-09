"""Reusable metric card renderer."""

from __future__ import annotations

import streamlit as st


def metric_card(title: str, value: str, subtitle: str | None = None) -> None:
    body = "<div style='padding:0.9rem;border:1px solid #d8e0e8;border-radius:12px;background:#f9fbfd;'>"
    body += f"<div style='font-size:0.85rem;color:#54616f;text-transform:uppercase;letter-spacing:0.06em;'>{title}</div>"
    body += f"<div style='font-size:1.5rem;font-weight:700;color:#0f1720;margin-top:0.2rem;'>{value}</div>"
    if subtitle:
        body += f"<div style='font-size:0.85rem;color:#687684;margin-top:0.3rem;'>{subtitle}</div>"
    body += "</div>"
    st.markdown(body, unsafe_allow_html=True)
