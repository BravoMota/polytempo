"""Streamlit performance viewer application."""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import streamlit as st

from polytempo.visualizer.csv_data import csv_mtime, run_export, visible_settlement_dates
from polytempo.visualizer.detail import render_trade_detail
from polytempo.visualizer.loaders import load_csv
from polytempo.visualizer.paths import DEFAULT_CSV, REPO_ROOT
from polytempo.visualizer.styling import inject_no_inner_scroll_css
from polytempo.visualizer.summary import (
    render_aggregated_chart,
    render_knob_summaries,
    render_pivot_table,
)


def main() -> None:
    default_path = DEFAULT_CSV
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        default_path = Path(sys.argv[1])

    st.set_page_config(page_title="Paper performance", layout="wide")
    inject_no_inner_scroll_css()
    st.title("Paper wallet performance")

    csv_path = st.sidebar.text_input("CSV path", value=str(default_path))
    path = Path(csv_path)
    if not path.is_absolute():
        path = REPO_ROOT / path

    st.sidebar.header("Data")
    if st.sidebar.button("Refresh from DB", type="primary"):
        with st.spinner("Exporting from Postgres (Gamma settlement dates)…"):
            ok, msg = run_export(path)
        if ok:
            st.cache_data.clear()
            st.sidebar.success(msg.splitlines()[-1] if msg else "Export complete")
            st.rerun()
        else:
            st.sidebar.error(msg)

    if not path.is_file():
        st.warning(
            f"No CSV at `{path}`. Click **Refresh from DB** in the sidebar to generate it."
        )
        st.stop()

    df, generated = load_csv(str(path), csv_mtime(path))
    if generated:
        st.caption(f"Snapshot: {generated}")

    data_min = df["settlement_date"].min()
    data_max = df["settlement_date"].max()
    dates_in_csv = (data_max - data_min).days + 1

    st.sidebar.header("Filters")
    models = st.sidebar.multiselect("model", sorted(df["model"].dropna().unique()))
    trades = st.sidebar.multiselect("trade", sorted(df["trade"].dropna().unique()))
    lead_values = sorted(
        int(float(x)) for x in df["lead_hours"].unique() if x and str(x).strip()
    )
    if lead_values:
        if len(lead_values) == 1:
            lead_lo = lead_hi = lead_values[0]
        else:
            lead_lo, lead_hi = st.sidebar.select_slider(
                "lead_hours",
                options=lead_values,
                value=(lead_values[0], lead_values[-1]),
            )
    else:
        lead_lo, lead_hi = None, None
    exits = st.sidebar.multiselect("exit_mode", sorted(df["exit_mode"].dropna().unique()))

    use_all_dates = st.sidebar.checkbox("all dates in CSV", value=False)
    if use_all_dates:
        days = dates_in_csv
        start_date = data_min
    else:
        days = st.sidebar.slider(
            "trailing days",
            1,
            max(dates_in_csv, 1),
            min(7, dates_in_csv),
        )
        start_date = data_max - timedelta(days=days - 1)

    filtered = df[df["settlement_date"].between(start_date, data_max)]
    if models:
        filtered = filtered[filtered["model"].isin(models)]
    if trades:
        filtered = filtered[filtered["trade"].isin(trades)]
    if lead_lo is not None and lead_hi is not None:
        allowed_leads = {str(v) for v in lead_values if lead_lo <= v <= lead_hi}
        filtered = filtered[filtered["lead_hours"].isin(allowed_leads)]
    if exits:
        filtered = filtered[filtered["exit_mode"].isin(exits)]

    aggregate = st.sidebar.checkbox("aggregate filtered wallets", value=False)

    if filtered.empty:
        st.warning("No rows match filters.")
        st.stop()

    n_dates_shown = filtered["settlement_date"].nunique()
    st.caption(
        f"CSV spans {data_min} → {data_max} ({dates_in_csv} settlement dates). "
        f"Showing {n_dates_shown} date column(s), {filtered['profile_id'].nunique()} wallet(s)."
    )

    render_knob_summaries(filtered)

    if aggregate:
        render_aggregated_chart(filtered)
    else:
        render_pivot_table(filtered, days=days, data_max=data_max)
        render_trade_detail(filtered, visible_settlement_dates(filtered))
