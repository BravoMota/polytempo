"""Streamlit performance viewer application."""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from polytempo.visualizer.csv_data import csv_mtime, run_export, visible_settlement_dates
from polytempo.visualizer.detail import render_trade_detail
from polytempo.visualizer.distribution_page import render_distribution_page
from polytempo.visualizer.loaders import load_csv
from polytempo.visualizer.paths import DEFAULT_CSV, REPO_ROOT
from polytempo.visualizer.prefs import (
    FilterPrefs,
    add_csv_preset,
    intersect_saved,
    knob_options,
    load_prefs,
    normalize_csv_preset,
    resolve_csv_preset,
    resolve_leads,
    resolve_models,
    save_filters,
)
from polytempo.visualizer.styling import inject_no_inner_scroll_css
from polytempo.visualizer.summary import (
    render_aggregated_chart,
    render_knob_summaries,
    render_pivot_table,
)

_SOURCE_DEFAULT = "__default__"
_SOURCE_CUSTOM = "__custom__"


def _default_csv_path() -> Path:
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        path = Path(sys.argv[1])
        return path if path.is_absolute() else REPO_ROOT / path
    return DEFAULT_CSV


def _resolve_user_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def _persist_filters() -> None:
    leads = st.session_state.get("perf_leads")
    lead_lo = lead_hi = None
    if isinstance(leads, (tuple, list)) and len(leads) == 2:
        lead_lo, lead_hi = int(leads[0]), int(leads[1])
    save_filters(
        FilterPrefs(
            models=list(st.session_state.get("perf_models") or []),
            trades=list(st.session_state.get("perf_trades") or []),
            lead_lo=lead_lo,
            lead_hi=lead_hi,
            exits=list(st.session_state.get("perf_exits") or []),
            budgets=list(st.session_state.get("perf_budgets") or []),
        )
    )


def _apply_saved_filters(df: pd.DataFrame) -> None:
    prefs = load_prefs()
    saved = prefs.filters
    model_options = knob_options(df["model"])
    trade_options = knob_options(df["trade"])
    exit_options = knob_options(df["exit_mode"]) if "exit_mode" in df.columns else []
    lead_values = sorted(
        int(float(x)) for x in df["lead_hours"].unique() if x and str(x).strip()
    )
    budget_col = (
        "event_budget"
        if "event_budget" in df.columns
        else ("sizing_mode" if "sizing_mode" in df.columns else None)
    )
    budget_options = []
    if budget_col is not None:
        budget_options = knob_options(df[budget_col])

    saved_models = None if saved is None else saved.models
    st.session_state.perf_models = resolve_models(saved_models, model_options)
    st.session_state.perf_trades = (
        [] if saved is None else intersect_saved(saved.trades, trade_options)
    )
    st.session_state.perf_exits = (
        [] if saved is None else intersect_saved(saved.exits, exit_options)
    )
    st.session_state.perf_budgets = (
        [] if saved is None else intersect_saved(saved.budgets, budget_options)
    )
    leads = resolve_leads(
        None if saved is None else saved.lead_lo,
        None if saved is None else saved.lead_hi,
        lead_values,
    )
    if leads is not None:
        st.session_state.perf_leads = leads


def _sync_filters_for_csv(df: pd.DataFrame, path: Path) -> None:
    """Re-apply saved knobs when the CSV (or its distinct values) changes."""
    model_options = tuple(knob_options(df["model"]))
    trade_options = tuple(knob_options(df["trade"]))
    lead_values = tuple(
        sorted(int(float(x)) for x in df["lead_hours"].unique() if x and str(x).strip())
    )
    sig = (str(path.resolve()), model_options, trade_options, lead_values)
    if st.session_state.get("perf_csv_sig") == sig:
        return
    st.session_state.perf_csv_sig = sig
    _apply_saved_filters(df)
    if load_prefs().filters is None:
        _persist_filters()


def _pick_csv_path(default_path: Path) -> Path:
    """Source picker. Always starts on the default; presets are opt-in."""
    prefs = load_prefs()
    default_label = normalize_csv_preset(default_path)
    presets = [p for p in prefs.csv_presets if p != default_label]
    labels = {
        _SOURCE_DEFAULT: f"default ({default_label})",
        **{preset: preset for preset in presets},
        _SOURCE_CUSTOM: "Custom path…",
    }
    choice = st.sidebar.selectbox(
        "CSV source",
        list(labels),
        format_func=lambda key: labels[key],
        index=0,
        key="csv_source",
        help="Opens on the default paper daily CSV. Previously used paths stay as presets.",
    )
    if choice == _SOURCE_CUSTOM:
        typed = st.sidebar.text_input("CSV path", key="csv_custom_path")
        if not typed.strip():
            st.info("Enter a CSV path, or pick a saved source above.")
            st.stop()
        return _resolve_user_path(typed)
    if choice == _SOURCE_DEFAULT:
        return default_path
    return resolve_csv_preset(choice)


def render_performance_page() -> None:
    default_path = _default_csv_path()

    inject_no_inner_scroll_css()
    st.title("Paper wallet performance")

    path = _pick_csv_path(default_path)

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

    if path.resolve() != default_path.resolve():
        add_csv_preset(path, default_csv=default_path)

    df, generated = load_csv(str(path), csv_mtime(path))
    if generated:
        st.caption(f"Snapshot: {generated}")

    _sync_filters_for_csv(df, path)

    data_min = df["settlement_date"].min()
    data_max = df["settlement_date"].max()
    dates_in_csv = (data_max - data_min).days + 1

    st.sidebar.header("Filters")
    models = st.sidebar.multiselect(
        "model",
        knob_options(df["model"]),
        key="perf_models",
        on_change=_persist_filters,
    )
    trades = st.sidebar.multiselect(
        "trade",
        knob_options(df["trade"]),
        key="perf_trades",
        on_change=_persist_filters,
    )
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
                key="perf_leads",
                on_change=_persist_filters,
            )
    else:
        lead_lo, lead_hi = None, None
    exits = st.sidebar.multiselect(
        "exit_mode",
        knob_options(df["exit_mode"]) if "exit_mode" in df.columns else [],
        key="perf_exits",
        on_change=_persist_filters,
    )
    budget_col = (
        "event_budget"
        if "event_budget" in df.columns
        else ("sizing_mode" if "sizing_mode" in df.columns else None)
    )
    budgets = []
    if budget_col is not None:
        budget_values = knob_options(df[budget_col])
        budgets = st.sidebar.multiselect(
            "event_budget",
            budget_values,
            key="perf_budgets",
            on_change=_persist_filters,
        )

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
    if budgets and budget_col is not None:
        filtered = filtered[filtered[budget_col].astype(str).isin(budgets)]

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


def main() -> None:
    st.set_page_config(page_title="PolyTempo", layout="wide")
    pages = [
        st.Page(render_performance_page, title="Paper performance", url_path="performance"),
        st.Page(render_distribution_page, title="Distribution", url_path="distribution"),
    ]
    st.navigation(pages).run()
