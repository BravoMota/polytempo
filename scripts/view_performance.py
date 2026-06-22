#!/usr/bin/env python3
"""Streamlit viewer for paper wallet performance CSV (offline, no DB).

Usage:
    streamlit run scripts/view_performance.py -- reports/performance/daily.csv
"""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = REPO_ROOT / "reports/performance/daily.csv"
RUN_WITH_ENV = REPO_ROOT / "deploy/bin/run-with-env.sh"
EXPORT_SCRIPT = REPO_ROOT / "scripts/report_performance.py"
ROW_HEIGHT = 35
HEADER_HEIGHT = 40


def _csv_mtime(path: Path) -> float:
    if not path.is_file():
        return -1.0
    return path.stat().st_mtime


def _read_csv_raw(path: Path) -> tuple[pd.DataFrame, str | None]:
    """Return dataframe and optional generated_utc from comment line."""
    generated: str | None = None
    with path.open(encoding="utf-8") as fh:
        first = fh.readline()
        if first.startswith("# generated_utc:"):
            generated = first.split(":", 1)[1].strip()
    df = pd.read_csv(path, comment="#")
    df["settlement_date"] = pd.to_datetime(df["settlement_date"]).dt.date
    if "since" in df.columns:
        df["since"] = pd.to_datetime(df["since"], errors="coerce").dt.date
    df["lead_hours"] = df["lead_hours"].apply(
        lambda v: "" if pd.isna(v) or str(v).strip() == "" else str(int(float(v)))
    )
    return df, generated


@st.cache_data(show_spinner=False)
def _load_csv(path_str: str, mtime: float) -> tuple[pd.DataFrame, str | None]:
    del mtime  # cache key only
    return _read_csv_raw(Path(path_str))


def _export_command(csv_path: Path) -> list[str]:
    return [
        str(RUN_WITH_ENV),
        str(EXPORT_SCRIPT),
        "--all",
        "--csv",
        str(csv_path),
    ]


def _run_export(csv_path: Path) -> tuple[bool, str]:
    if not RUN_WITH_ENV.is_file():
        return False, f"Missing {RUN_WITH_ENV}"
    if not EXPORT_SCRIPT.is_file():
        return False, f"Missing {EXPORT_SCRIPT}"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        _export_command(csv_path),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "export failed").strip()
        tail = "\n".join(err.splitlines()[-20:])
        return False, tail
    msg = (result.stdout or "export ok").strip()
    return True, msg


def _fmt_pct(v: float) -> str:
    if pd.isna(v):
        return "·"
    if abs(v) < 0.05:
        return "0.0"
    return f"{v:+.1f}"


def _period_pnl_pct(sub: pd.DataFrame) -> float:
    sod = float(sub["sod_balance_usd"].sum())
    if sod <= 0:
        return 0.0
    return 100.0 * float(sub["pnl_usd"].sum()) / sod


def _knob_summary(filtered: pd.DataFrame, column: str, label: str) -> pd.DataFrame:
    rows: list[dict] = []
    for value in sorted(filtered[column].dropna().unique(), key=str):
        if value == "":
            continue
        sub = filtered[filtered[column] == value]
        rows.append(
            {
                label: value,
                "Σ%": _period_pnl_pct(sub),
                "wallets": sub["profile_id"].nunique(),
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values("Σ%", ascending=False)


def _knob_vabs(filtered: pd.DataFrame) -> float:
    values: list[float] = []
    for column in ("model", "trade", "lead_hours", "exit_mode"):
        summary = _knob_summary(filtered, column, column)
        if not summary.empty:
            values.extend(summary["Σ%"].tolist())
    if not values:
        return 1.0
    return min(max(1.0, max(abs(v) for v in values)), 50.0)


def _style_knob_table(
    summary: pd.DataFrame,
    label_col: str,
    vabs: float,
) -> pd.io.formats.style.Styler:
    display = summary[[label_col, "Σ%", "wallets"]].rename(columns={"Σ%": "Σ"})

    def _row_style(row: pd.Series) -> list[str]:
        return ["", _cell_bg(row["Σ"], vabs), ""]

    return (
        display.style.apply(_row_style, axis=1)
        .format("{:+.1f}", na_rep="·", subset=["Σ"])
    )


def _render_knob_summaries(filtered: pd.DataFrame) -> None:
    st.subheader("Period P/L by knob")
    vabs = _knob_vabs(filtered)
    cols = st.columns(4)
    knobs = [
        ("model", "model"),
        ("trade", "trade"),
        ("lead_hours", "lead"),
        ("exit_mode", "exit"),
    ]
    for col_widget, (column, label) in zip(cols, knobs):
        summary = _knob_summary(filtered, column, label)
        with col_widget:
            st.caption(label)
            if summary.empty:
                st.write("—")
            else:
                styled = _style_knob_table(summary, label, vabs)
                st.dataframe(
                    styled,
                    hide_index=True,
                    width="stretch",
                    height=_table_height(len(summary)),
                    row_height=ROW_HEIGHT,
                )


def _inject_no_inner_scroll_css() -> None:
    st.markdown(
        """
        <style>
        div[data-testid="stDataFrame"] > div {
            overflow: visible !important;
            max-height: none !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _table_height(n_rows: int) -> int:
    return HEADER_HEIGHT + max(n_rows, 1) * ROW_HEIGHT


def _cell_bg(val: object, vmax: float) -> str:
    """Red (loss) → black (flat) → green (gain)."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "background-color: #2a2a2a; color: #888888"
    try:
        v = float(val)
    except (TypeError, ValueError):
        return ""
    t = max(-1.0, min(1.0, v / vmax))
    if abs(t) < 0.02:
        return "background-color: #000000; color: #ffffff"
    if t > 0:
        g = int(255 * t)
        r = b = 0
    else:
        r = int(255 * (-t))
        g = b = 0
    lum = r + g + b
    fg = "#ffffff" if lum < 200 else "#000000"
    return f"background-color: rgb({r},{g},{b}); color: {fg}"


def _pivot_with_total(filtered: pd.DataFrame) -> pd.io.formats.style.Styler:
    pivot = filtered.pivot_table(
        index="profile_id",
        columns="settlement_date",
        values="pnl_pct",
        aggfunc="sum",
    )
    col_dates = sorted(pivot.columns)
    pivot = pivot[col_dates]
    pivot["Σ%"] = pivot.sum(axis=1, skipna=True)
    pivot = pivot.sort_values("Σ%", ascending=False)

    date_labels = [d.isoformat() for d in col_dates]
    pivot = pivot.rename(columns={d: label for d, label in zip(col_dates, date_labels)})
    out = pivot.reset_index().rename(columns={"profile_id": "wallet"})
    out = out[["wallet", "Σ%"] + date_labels]

    value_cols = ["Σ%"] + date_labels
    vabs = max(1.0, float(out[value_cols].abs().max().max(skipna=True)))
    vabs = min(vabs, 50.0)

    def _row_style(row: pd.Series) -> list[str]:
        return [
            _cell_bg(row[c], vabs) if c in value_cols else ""
            for c in out.columns
        ]

    return (
        out.style.apply(_row_style, axis=1)
        .format("{:+.1f}", na_rep="·", subset=value_cols)
    )


def main() -> None:
    default_path = DEFAULT_CSV
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        default_path = Path(sys.argv[1])

    st.set_page_config(page_title="Paper performance", layout="wide")
    _inject_no_inner_scroll_css()
    st.title("Paper wallet performance")

    csv_path = st.sidebar.text_input("CSV path", value=str(default_path))
    path = Path(csv_path)
    if not path.is_absolute():
        path = REPO_ROOT / path

    st.sidebar.header("Data")
    if st.sidebar.button("Refresh from DB", type="primary"):
        with st.spinner("Exporting from Postgres (Gamma settlement dates)…"):
            ok, msg = _run_export(path)
        if ok:
            st.cache_data.clear()
            st.sidebar.success(msg.splitlines()[-1] if msg else "Export complete")
            st.rerun()
        else:
            st.sidebar.error(msg)

    if not path.is_file():
        st.warning(f"No CSV at `{path}`. Click **Refresh from DB** in the sidebar to generate it.")
        st.stop()

    df, generated = _load_csv(str(path), _csv_mtime(path))
    if generated:
        st.caption(f"Snapshot: {generated}")

    data_min = df["settlement_date"].min()
    data_max = df["settlement_date"].max()
    dates_in_csv = (data_max - data_min).days + 1

    st.sidebar.header("Filters")
    models = st.sidebar.multiselect("model", sorted(df["model"].dropna().unique()))
    trades = st.sidebar.multiselect("trade", sorted(df["trade"].dropna().unique()))
    lead_opts = sorted(
        [x for x in df["lead_hours"].unique() if x],
        key=lambda x: int(x),
    )
    leads = st.sidebar.multiselect("lead_hours", lead_opts)
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
    if leads:
        filtered = filtered[filtered["lead_hours"].isin(leads)]
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

    _render_knob_summaries(filtered)

    if aggregate:
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
            pnl_pct=agg["pnl_pct"].map(_fmt_pct),
        )
        st.dataframe(
            agg_display,
            hide_index=True,
            width="stretch",
            height=_table_height(len(agg_display)),
            row_height=ROW_HEIGHT,
        )
    else:
        styled = _pivot_with_total(filtered)
        n_rows = len(styled.data)
        st.subheader(f"Daily P/L % ({days}d ending {data_max})")
        st.dataframe(
            styled,
            width="stretch",
            height=_table_height(n_rows),
            row_height=ROW_HEIGHT,
            hide_index=True,
            column_config={
                "wallet": st.column_config.TextColumn("wallet", pinned=True),
                "Σ%": st.column_config.TextColumn("Σ%", pinned=True),
            },
        )


main()
