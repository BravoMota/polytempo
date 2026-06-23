"""Summary tables: period P/L by knob and wallet × date pivot."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from polytempo.visualizer.chart import build_pivot_dataframe, pivot_cell_to_wallet_date
from polytempo.visualizer.csv_data import fmt_pct, period_pnl_pct
from polytempo.visualizer.paths import ROW_HEIGHT
from polytempo.visualizer.styling import (
    style_knob_table,
    style_pivot_dataframe,
    table_height,
)


def knob_summary(filtered: pd.DataFrame, column: str, label: str) -> pd.DataFrame:
    rows: list[dict] = []
    for value in sorted(filtered[column].dropna().unique(), key=str):
        if value == "":
            continue
        sub = filtered[filtered[column] == value]
        rows.append(
            {
                label: value,
                "Σ%": period_pnl_pct(sub),
                "wallets": sub["profile_id"].nunique(),
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values("Σ%", ascending=False)


def knob_vabs(filtered: pd.DataFrame) -> float:
    values: list[float] = []
    for column in ("model", "trade", "lead_hours", "exit_mode"):
        summary = knob_summary(filtered, column, column)
        if not summary.empty:
            values.extend(summary["Σ%"].tolist())
    if not values:
        return 1.0
    return min(max(1.0, max(abs(v) for v in values)), 50.0)


def render_knob_summaries(filtered: pd.DataFrame) -> None:
    st.subheader("Period P/L by knob")
    vabs = knob_vabs(filtered)
    cols = st.columns(4)
    knobs = [
        ("model", "model"),
        ("trade", "trade"),
        ("lead_hours", "lead"),
        ("exit_mode", "exit"),
    ]
    for col_widget, (column, label) in zip(cols, knobs):
        summary = knob_summary(filtered, column, label)
        with col_widget:
            st.caption(label)
            if summary.empty:
                st.write("—")
            else:
                styled = style_knob_table(summary, label, vabs)
                st.dataframe(
                    styled,
                    hide_index=True,
                    width="stretch",
                    height=table_height(len(summary)),
                    row_height=ROW_HEIGHT,
                )


def apply_cell_selection(
    widget_key: str,
    pivot_df: pd.DataFrame,
    date_labels: list[str],
) -> None:
    """Read st.dataframe single-cell selection and pre-fill trade detail."""
    state = st.session_state.get(widget_key)
    if state is None:
        return
    selection = getattr(state, "selection", None)
    if selection is None and isinstance(state, dict):
        selection = state.get("selection")
    if selection is None:
        return

    row_idx: int | None = None
    col_name: str | None = None

    cells = getattr(selection, "cells", None)
    if cells is None and isinstance(selection, dict):
        cells = selection.get("cells")
    if cells:
        cell = cells[0]
        if isinstance(cell, (list, tuple)) and len(cell) == 2:
            row_idx = int(cell[0])
            col_name = str(cell[1])
    if row_idx is None or col_name is None:
        rows = getattr(selection, "rows", None)
        cols = getattr(selection, "columns", None)
        if isinstance(selection, dict):
            rows = rows or selection.get("rows")
            cols = cols or selection.get("columns")
        if rows and cols:
            row_idx = int(rows[0])
            col_name = str(cols[0])

    if row_idx is None or col_name is None:
        return

    mapped = pivot_cell_to_wallet_date(row_idx, col_name, pivot_df, date_labels)
    if mapped is None:
        return
    wallet, settlement_date = mapped
    if (
        st.session_state.get("detail_wallet") == wallet
        and st.session_state.get("detail_date") == settlement_date
        and st.session_state.get("detail_expanded")
    ):
        return
    st.session_state["detail_wallet"] = wallet
    st.session_state["detail_date"] = settlement_date
    st.session_state["detail_expanded"] = True
    st.session_state["scroll_to_detail"] = True


def render_pivot_table(filtered: pd.DataFrame, *, days: int, data_max: date) -> None:
    pivot_df, date_labels, vabs = build_pivot_dataframe(filtered)
    styled = style_pivot_dataframe(pivot_df, date_labels, vabs)
    n_rows = len(pivot_df)
    st.subheader(f"Daily P/L % ({days}d ending {data_max})")
    st.caption("Click a date cell to open trade detail below.")
    st.dataframe(
        styled,
        on_select="rerun",
        selection_mode="single-cell",
        key="pnl_pivot",
        hide_index=True,
        width="stretch",
        height=table_height(n_rows),
        row_height=ROW_HEIGHT,
        column_config={
            "wallet": st.column_config.TextColumn("wallet", pinned=True),
            "Σ%": st.column_config.TextColumn("Σ%", pinned=True),
        },
    )
    apply_cell_selection("pnl_pivot", pivot_df, date_labels)


def render_aggregated_chart(filtered: pd.DataFrame) -> None:
    agg = filtered.groupby("settlement_date", as_index=False).agg(
        pnl_usd=("pnl_usd", "sum"),
        sod_balance_usd=("sod_balance_usd", "sum"),
    )
    agg["pnl_pct"] = 100.0 * agg["pnl_usd"] / agg["sod_balance_usd"].replace(0, pd.NA)
    st.subheader("Aggregated daily P/L %")
    chart_df = agg.set_index("settlement_date")["pnl_pct"]
    st.line_chart(chart_df)
    agg_display = agg.assign(
        settlement_date=agg["settlement_date"].astype(str),
        pnl_pct=agg["pnl_pct"].map(fmt_pct),
    )
    st.dataframe(
        agg_display,
        hide_index=True,
        width="stretch",
        height=table_height(len(agg_display)),
        row_height=ROW_HEIGHT,
    )
