"""Plotly figures and pivot tables for the performance viewer."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import pandas as pd
import plotly.graph_objects as go

from polytempo.analysis import AnalysisResult
from polytempo.markets.buckets import TemperatureBucket, parse_temperature_bucket
from polytempo.markets.polymarket import PolymarketEvent
from polytempo.model.distribution import normal_pdf
from polytempo.visualizer.trades import P_FROM_TRADES_COL

_HEATMAP_COLORSCALE = [
    [0.0, "rgb(255,0,0)"],
    [0.5, "rgb(0,0,0)"],
    [1.0, "rgb(0,255,0)"],
]


def build_pivot_dataframe(filtered: pd.DataFrame) -> tuple[pd.DataFrame, list[str], float]:
    """Return display pivot, date column labels (ISO), and color scale vmax."""
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
    return out, date_labels, vabs


def _color_z(val: float | None, vabs: float) -> float | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    t = max(-1.0, min(1.0, float(val) / vabs))
    return (t + 1.0) / 2.0


def build_pivot_heatmap(
    pivot_df: pd.DataFrame,
    date_labels: list[str],
    vabs: float,
) -> go.Figure:
    """Clickable wallet × date heatmap (date columns only, not Σ%)."""
    wallets = pivot_df["wallet"].tolist()
    z: list[list[float | None]] = []
    text: list[list[str]] = []
    for _, row in pivot_df.iterrows():
        z_row: list[float | None] = []
        text_row: list[str] = []
        for col in date_labels:
            raw = row.get(col)
            if raw is None or (isinstance(raw, float) and pd.isna(raw)):
                z_row.append(None)
                text_row.append("·")
            else:
                z_row.append(_color_z(float(raw), vabs))
                text_row.append(f"{float(raw):+.1f}")
        z.append(z_row)
        text.append(text_row)

    fig = go.Figure(
        data=go.Heatmap(
            z=z,
            x=date_labels,
            y=wallets,
            text=text,
            texttemplate="%{text}",
            hovertemplate="wallet=%{y}<br>date=%{x}<br>P/L %%=%{text}<extra></extra>",
            colorscale=_HEATMAP_COLORSCALE,
            zmin=0,
            zmax=1,
            showscale=False,
        )
    )
    fig.update_layout(
        title="Daily P/L % (click a cell for trade detail)",
        xaxis_title="settlement date",
        yaxis_title="wallet",
        yaxis=dict(autorange="reversed"),
        height=max(400, 28 * len(wallets)),
        margin=dict(l=120, r=20, t=40, b=40),
    )
    return fig


def pivot_click_to_wallet_date(
    point: dict,
    wallets: list[str],
    date_labels: list[str],
) -> tuple[str, date] | None:
    """Map a Plotly heatmap click to wallet + settlement date."""
    wallet = point.get("y")
    date_label = point.get("x")
    if wallet is None or date_label is None:
        return None
    wallet_s = str(wallet)
    date_s = str(date_label)
    if wallet_s not in wallets or date_s not in date_labels:
        return None
    return wallet_s, date.fromisoformat(date_s)


def pivot_cell_to_wallet_date(
    row_idx: int,
    column_name: str,
    pivot_df: pd.DataFrame,
    date_labels: list[str],
) -> tuple[str, date] | None:
    """Map a st.dataframe cell selection to wallet + settlement date."""
    if column_name in ("wallet", "Σ%"):
        return None
    if column_name not in date_labels:
        return None
    if row_idx < 0 or row_idx >= len(pivot_df):
        return None
    wallet = str(pivot_df.iloc[row_idx]["wallet"])
    return wallet, date.fromisoformat(column_name)


@dataclass(frozen=True)
class MarketImpliedSummary:
    """Discrete market-implied location/spread from normalized yes_ask weights."""

    mean_c: float
    discrete_std_c: float


@dataclass(frozen=True)
class AnalysisChartBundle:
    figure: go.Figure
    market: MarketImpliedSummary | None


def bucket_center_c(bucket: TemperatureBucket) -> float:
    if bucket.lower_c is not None and bucket.upper_c is not None:
        return (bucket.lower_c + bucket.upper_c) / 2.0
    if bucket.lower_c is not None:
        return bucket.lower_c + 0.5
    if bucket.upper_c is not None:
        return bucket.upper_c - 0.5
    raise ValueError(f"bucket has no finite bounds: {bucket.label!r}")


def compute_market_implied_summary(
    labels: list[str],
    yes_asks: list[float | None],
) -> MarketImpliedSummary | None:
    """Weighted mean/std of bucket centers using normalized yes_ask masses."""
    centers: list[float] = []
    weights: list[float] = []
    for label, ask in zip(labels, yes_asks, strict=True):
        if ask is None or ask <= 0:
            continue
        bucket = parse_temperature_bucket(label)
        centers.append(bucket_center_c(bucket))
        weights.append(float(ask))
    if not weights:
        return None
    total = sum(weights)
    norm = [w / total for w in weights]
    mean = sum(w * c for w, c in zip(norm, centers, strict=True))
    var = sum(w * (c - mean) ** 2 for w, c in zip(norm, centers, strict=True))
    return MarketImpliedSummary(mean_c=mean, discrete_std_c=math.sqrt(var))


def build_analysis_chart(
    analysis: AnalysisResult,
    event: PolymarketEvent,
    *,
    traded_bucket_labels: set[str] | None = None,
    resolution_label: str | None = None,
) -> AnalysisChartBundle:
    """Bucket bars, model PDF, and market-implied moments on one °C axis."""
    traded = traded_bucket_labels or set()
    labels = [row.label for row in analysis.rows]
    market = [row.yes_ask if row.yes_ask is not None else 0.0 for row in analysis.rows]
    model_p = [row.probability for row in analysis.rows]
    market_summary = compute_market_implied_summary(labels, market)

    mu = analysis.distribution_mean_c
    sigma = analysis.distribution_sigma_c
    buckets = [parse_temperature_bucket(lbl) for lbl in labels]
    centers = [bucket_center_c(b) for b in buckets]
    bar_offset = 0.12

    bounds: list[float] = []
    for bucket in buckets:
        if bucket.lower_c is not None:
            bounds.append(bucket.lower_c)
        if bucket.upper_c is not None:
            bounds.append(bucket.upper_c)
    if not bounds:
        bounds = [mu - 3 * sigma, mu + 3 * sigma]
    x_min = min(bounds) - 2 * sigma
    x_max = max(bounds) + 2 * sigma
    n_pts = 200
    xs = [x_min + (x_max - x_min) * i / (n_pts - 1) for i in range(n_pts)]
    pdf_ys = [normal_pdf(x, mu, sigma) for x in xs]

    market_colors = [
        "#4da6ff" if lbl in traded else "#336699" for lbl in labels
    ]
    model_colors = [
        "#ffcc00" if lbl in traded else "#b38600" for lbl in labels
    ]

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            name="yes_ask",
            x=[c - bar_offset for c in centers],
            y=market,
            marker_color=market_colors,
            text=[f"{v:.3f}" if v else "" for v in market],
            textposition="outside",
            hovertemplate="bucket=%{customdata}<br>yes_ask=%{y:.3f}<extra></extra>",
            customdata=labels,
        )
    )
    fig.add_trace(
        go.Bar(
            name="model P",
            x=[c + bar_offset for c in centers],
            y=model_p,
            marker_color=model_colors,
            text=[f"{v:.3f}" for v in model_p],
            textposition="outside",
            hovertemplate="bucket=%{customdata}<br>model P=%{y:.3f}<extra></extra>",
            customdata=labels,
        )
    )
    fig.add_trace(
        go.Scatter(
            x=xs,
            y=pdf_ys,
            mode="lines",
            name="model PDF",
            line=dict(color="#ffcc00", width=2),
            hovertemplate="°C=%{x:.2f}<br>density=%{y:.3f}<extra></extra>",
        )
    )
    for bucket in buckets:
        for edge in (bucket.lower_c, bucket.upper_c):
            if edge is not None:
                fig.add_vline(
                    x=edge,
                    line_width=1,
                    line_dash="dot",
                    line_color="#666666",
                )

    if resolution_label and resolution_label in labels:
        fig.add_vline(
            x=centers[labels.index(resolution_label)],
            line_width=3,
            line_color="#66ff99",
            layer="above",
        )

    fig.add_vline(
        x=mu,
        line_width=2.5,
        line_color="#ffcc00",
        layer="above",
    )
    if market_summary is not None:
        fig.add_vline(
            x=market_summary.mean_c,
            line_width=2.5,
            line_color="#4da6ff",
            layer="above",
        )

    fig.update_xaxes(
        tickmode="array",
        tickvals=centers,
        ticktext=labels,
        tickangle=-45 if len(labels) > 7 else 0,
    )

    fig.update_layout(
        barmode="group",
        height=420,
        margin=dict(l=40, r=20, t=30, b=80 if len(labels) > 7 else 60),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        xaxis_title="°C (bucket centers)",
        yaxis_title="probability / density",
    )
    return AnalysisChartBundle(figure=fig, market=market_summary)


def bucket_table_dataframe(
    analysis: AnalysisResult,
    *,
    trade_p_by_bucket: dict[str, float] | None = None,
) -> pd.DataFrame:
    """Per-bucket model P, market yes_ask, and edge."""
    trade_p = trade_p_by_bucket or {}
    rows: list[dict[str, object]] = []
    for row in analysis.rows:
        edge = row.probability - row.yes_ask if row.yes_ask is not None else None
        p_trade = trade_p.get(row.label)
        rows.append(
            {
                "bucket": row.label,
                P_FROM_TRADES_COL: p_trade,
                "P": row.probability,
                "yes_ask": row.yes_ask,
                "edge": edge,
            }
        )
    return pd.DataFrame(rows)


def bucket_table_has_replay_mismatch(
    df: pd.DataFrame,
    *,
    tolerance: float = 1e-4,
) -> bool:
    """True when ledger P and replay P differ for any bucket with both values."""
    if P_FROM_TRADES_COL not in df.columns or "P" not in df.columns:
        return False
    for p_trade, p_replay in zip(df[P_FROM_TRADES_COL], df["P"], strict=True):
        if p_trade is None or (isinstance(p_trade, float) and pd.isna(p_trade)):
            continue
        if p_replay is None or (isinstance(p_replay, float) and pd.isna(p_replay)):
            continue
        if abs(float(p_trade) - float(p_replay)) > tolerance:
            return True
    return False


def model_buckets_dataframe(analysis: AnalysisResult) -> pd.DataFrame:
    """Per-bucket model probabilities and edge vs market yes_ask."""
    rows: list[dict[str, object]] = []
    for row in analysis.rows:
        edge = row.probability - row.yes_ask if row.yes_ask is not None else None
        rows.append(
            {
                "bucket": row.label,
                "P": row.probability,
                "edge": edge,
            }
        )
    return pd.DataFrame(rows)


def market_buckets_dataframe(analysis: AnalysisResult) -> pd.DataFrame:
    """Per-bucket market yes_ask prices."""
    return pd.DataFrame(
        [{"bucket": row.label, "yes_ask": row.yes_ask} for row in analysis.rows]
    )
