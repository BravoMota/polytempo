"""Table styling helpers for the Streamlit performance viewer."""

from __future__ import annotations

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from polytempo.visualizer.paths import HEADER_HEIGHT, ROW_HEIGHT

TRADE_DETAIL_ANCHOR = "trade-detail-anchor"


def inject_no_inner_scroll_css() -> None:
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


def table_height(n_rows: int) -> int:
    return HEADER_HEIGHT + max(n_rows, 1) * ROW_HEIGHT


def scroll_to_anchor(anchor_id: str) -> None:
    """Smooth-scroll the parent page to an element id (Streamlit iframe)."""
    nonce = st.session_state.get("_scroll_nonce", 0)
    st.session_state["_scroll_nonce"] = nonce + 1
    components.html(
        f"""
        <script>
            (function() {{
                function scroll() {{
                    const doc = window.parent.document;
                    const el = doc.getElementById("{anchor_id}");
                    if (el) {{
                        el.scrollIntoView({{behavior: "smooth", block: "start"}});
                    }}
                }}
                scroll();
                requestAnimationFrame(scroll);
            }})();
        </script>
        <!-- scroll nonce: {nonce} -->
        """,
        height=0,
    )


def cell_bg(val: object, vmax: float) -> str:
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


def _scale_t(val: object, vmax: float) -> float | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        v = float(val)
    except (TypeError, ValueError):
        return None
    if vmax <= 0:
        return 0.0
    return max(0.0, min(1.0, v / vmax))


def black_to_yellow_bg(val: object, vmax: float) -> str:
    """Black (0) → yellow (max) for probability-style values."""
    t = _scale_t(val, vmax)
    if t is None:
        return "background-color: #2a2a2a; color: #888888"
    if t < 0.02:
        return "background-color: #000000; color: #ffffff"
    level = int(255 * t)
    fg = "#000000" if t > 0.55 else "#ffffff"
    return f"background-color: rgb({level},{level},0); color: {fg}"


def black_to_blue_bg(val: object, vmax: float) -> str:
    """Black (0) → blue (max) for market yes_ask values."""
    t = _scale_t(val, vmax)
    if t is None:
        return "background-color: #2a2a2a; color: #888888"
    if t < 0.02:
        return "background-color: #000000; color: #ffffff"
    level = int(255 * t)
    return f"background-color: rgb(0,0,{level}); color: #ffffff"


def style_bucket_table(df: pd.DataFrame) -> pd.io.formats.style.Styler:
    """Gradient styling for merged bucket replay table."""
    p_max = max(float(df["P"].max(skipna=True) or 0.0), 0.01)
    ask_max = max(float(df["yes_ask"].max(skipna=True) or 0.0), 0.01)
    edge_abs = df["edge"].abs().max(skipna=True)
    edge_vmax = max(float(edge_abs) if edge_abs is not None and not pd.isna(edge_abs) else 0.0, 0.01)

    def _row_style(row: pd.Series) -> list[str]:
        styles: list[str] = []
        for col in df.columns:
            if col == "P":
                styles.append(black_to_yellow_bg(row[col], p_max))
            elif col == "yes_ask":
                styles.append(black_to_blue_bg(row[col], ask_max))
            elif col == "edge":
                styles.append(cell_bg(row[col], edge_vmax))
            else:
                styles.append("")
        return styles

    return (
        df.style.apply(_row_style, axis=1)
        .format({"P": "{:.4f}", "yes_ask": "{:.4f}", "edge": "{:+.4f}"}, na_rep="·")
    )


def style_pivot_dataframe(
    pivot_df: pd.DataFrame,
    date_labels: list[str],
    vabs: float,
) -> pd.io.formats.style.Styler:
    value_cols = ["Σ%"] + date_labels

    def _row_style(row: pd.Series) -> list[str]:
        return [
            cell_bg(row[c], vabs) if c in value_cols else ""
            for c in pivot_df.columns
        ]

    return (
        pivot_df.style.apply(_row_style, axis=1)
        .format("{:+.1f}", na_rep="·", subset=value_cols)
    )


def style_knob_table(
    summary: pd.DataFrame,
    label_col: str,
    vabs: float,
) -> pd.io.formats.style.Styler:
    display = summary[[label_col, "Σ%", "wallets"]].rename(columns={"Σ%": "Σ"})

    def _row_style(row: pd.Series) -> list[str]:
        return ["", cell_bg(row["Σ"], vabs), ""]

    return (
        display.style.apply(_row_style, axis=1)
        .format("{:+.1f}", na_rep="·", subset=["Σ"])
    )
