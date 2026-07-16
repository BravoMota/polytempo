"""Single overlay chart for the Distribution Explorer page.

Two stacked panels share one °C x-axis so densities and probabilities never fight
for the same y-scale:

* top  — continuous PDFs (metadata models + strategies) with mean lines
* bottom — grouped per-bucket probability bars (market + strategies)

Each panel is shown only when it has content, so any toggle combination stays
legible. Reuses the trade-detail chart's visual language from ``chart.py``.
"""

from __future__ import annotations

import statistics

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from polytempo.markets.buckets import parse_temperature_bucket
from polytempo.model.distribution import normal_pdf
from polytempo.visualizer.bucket_math import bucket_center_c
from polytempo.visualizer.distribution_data import DistributionView

STRAT_COLORS = {
    "ensemble_spread": "#ff7f0e",
    "best_historical": "#2ca02c",
    "best_historical_updated": "#17becf",
    "weighted_historical_updated": "#e377c2",
    "weighted_historical_market_sigma": "#bcbd22",
    "weighted_historical_updated_sharp": "#8c564b",
}
FORECAST_COLORS = {"wunderground": "#9467bd"}
_MARKET_COLOR = "#4da6ff"
_RESOLVED_COLOR = "#66ff99"
_EDGE_COLOR = "#555555"
MODEL_PALETTE = [
    "#ffd24d", "#c2c2c2", "#9edae5", "#f7b6d2", "#dbdb8d",
    "#c5b0d5", "#ff9896", "#98df8a", "#aec7e8", "#ffbb78",
]


def _center_spacing(centers: list[float]) -> float:
    ordered = sorted(centers)
    diffs = [b - a for a, b in zip(ordered, ordered[1:]) if b > a]
    return statistics.median(diffs) if diffs else 1.0


def _pdf_xrange(
    centers: list[float],
    bounds: list[float],
    means_sigmas: list[tuple[float, float]],
) -> tuple[float, float]:
    lo: list[float] = [*bounds, *centers]
    hi: list[float] = [*bounds, *centers]
    for mean, sigma in means_sigmas:
        lo.append(mean - 3 * sigma)
        hi.append(mean + 3 * sigma)
    if not lo:
        return -5.0, 35.0
    pad = 0.4
    return min(lo) - pad, max(hi) + pad


def _empty_figure(message: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(
        text=message, showarrow=False, font=dict(size=16, color="#888888"),
        xref="paper", yref="paper", x=0.5, y=0.5,
    )
    fig.update_layout(height=320, xaxis=dict(visible=False), yaxis=dict(visible=False))
    return fig


def _strat_label(o) -> str:
    if o.selected_model:
        return f"{o.strategy} · {o.selected_model} (μ{o.mean_c:.1f} σ{o.sigma_c:.1f})"
    return f"{o.strategy} (μ{o.mean_c:.1f} σ{o.sigma_c:.1f})"


def build_distribution_chart(
    view: DistributionView,
    *,
    show_models: bool,
    show_strats: bool,
    strat_filter: set[str],
    show_market: bool,
    show_resolved: bool,
) -> go.Figure:
    """Stacked density + probability panels for the selected overlays."""
    buckets = [parse_temperature_bucket(lbl) for lbl in view.bucket_labels]
    centers = [bucket_center_c(b) for b in buckets]
    center_by_label = dict(zip(view.bucket_labels, centers))

    bounds: list[float] = []
    for b in buckets:
        if b.lower_c is not None:
            bounds.append(b.lower_c)
        if b.upper_c is not None:
            bounds.append(b.upper_c)
    bucket_edges = sorted({e for b in buckets for e in (b.lower_c, b.upper_c) if e is not None})

    active_strats = [
        o for o in view.strat_overlays if show_strats and o.strategy in strat_filter
    ]
    model_active = show_models and bool(view.model_overlays)
    resolved_x = (
        center_by_label.get(view.resolved_label)
        if show_resolved and view.resolved_label
        else None
    )

    means_sigmas: list[tuple[float, float]] = []
    if model_active:
        means_sigmas += [(o.mean_c, o.sigma_c) for o in view.model_overlays]
    means_sigmas += [(o.mean_c, o.sigma_c) for o in active_strats]
    x_min, x_max = _pdf_xrange(centers, bounds, means_sigmas)
    n_pts = 240
    xs = [x_min + (x_max - x_min) * i / (n_pts - 1) for i in range(n_pts)]

    want_pdf = model_active or bool(active_strats)
    want_bar = (show_market and view.market is not None) or bool(active_strats)

    panels = [name for name, want in (("pdf", want_pdf), ("bar", want_bar)) if want]
    if not panels:
        return _empty_figure("No overlays selected — enable one in the sidebar.")

    row_of = {name: i + 1 for i, name in enumerate(panels)}
    titles = {
        "pdf": "Distribution density",
        "bar": "Per-bucket probability",
    }
    row_heights = [0.6, 0.4] if len(panels) == 2 else [1.0]
    fig = make_subplots(
        rows=len(panels),
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.09,
        row_heights=row_heights,
        subplot_titles=[titles[p] for p in panels],
    )

    # ---- density panel: model + strategy PDFs ------------------------------
    if "pdf" in row_of:
        r = row_of["pdf"]
        if model_active:
            for idx, o in enumerate(view.model_overlays):
                color = FORECAST_COLORS.get(o.model, MODEL_PALETTE[idx % len(MODEL_PALETTE)])
                fig.add_trace(
                    go.Scatter(
                        x=xs, y=[normal_pdf(x, o.mean_c, o.sigma_c) for x in xs],
                        mode="lines", name=f"{o.model} (μ{o.mean_c:.1f} σ{o.sigma_c:.1f})",
                        line=dict(color=color, width=1.3), opacity=0.8,
                        legendgroup="models", legendgrouptitle_text="Forecasts",
                        hovertemplate=f"{o.model}<br>%{{x:.2f}}°C · %{{y:.3f}}<extra></extra>",
                    ),
                    row=r, col=1,
                )
                fig.add_vline(x=o.mean_c, line_width=1, line_color=color, opacity=0.35, row=r, col=1)
        for o in active_strats:
            color = STRAT_COLORS.get(o.strategy, "#888888")
            fig.add_trace(
                go.Scatter(
                    x=xs, y=[normal_pdf(x, o.mean_c, o.sigma_c) for x in xs],
                    mode="lines", name=_strat_label(o),
                    line=dict(color=color, width=2.6),
                    legendgroup="strats", legendgrouptitle_text="Strategies",
                    hovertemplate=f"{o.strategy}<br>%{{x:.2f}}°C · %{{y:.3f}}<extra></extra>",
                ),
                row=r, col=1,
            )
            fig.add_vline(x=o.mean_c, line_width=2, line_color=color, line_dash="dash", row=r, col=1)
        fig.update_yaxes(title_text="density", row=r, col=1)

    # ---- probability panel: grouped bucket bars ----------------------------
    if "bar" in row_of:
        r = row_of["bar"]
        bar_series: list[tuple[str, list[float], str, str]] = []
        if show_market and view.market is not None:
            asks = [a if a is not None else 0.0 for _, a in view.market.bucket_asks]
            bar_series.append(("market yes_ask", asks, _MARKET_COLOR, "market"))
        for o in active_strats:
            by_label = dict(o.bucket_probs)
            aligned = [by_label.get(lbl, 0.0) for lbl in view.bucket_labels]
            bar_series.append(
                (_strat_label(o), aligned, STRAT_COLORS.get(o.strategy, "#888888"), "strats")
            )

        if bar_series and centers:
            spacing = _center_spacing(centers)
            total_w = 0.72 * spacing
            bar_w = total_w / len(bar_series)
            for i, (name, values, color, group) in enumerate(bar_series):
                offset = -total_w / 2 + (i + 0.5) * bar_w
                fig.add_trace(
                    go.Bar(
                        name=name, x=[c + offset for c in centers], y=values,
                        width=bar_w * 0.92, marker_color=color, opacity=0.85,
                        legendgroup=group, showlegend="pdf" not in row_of or group == "market",
                        customdata=view.bucket_labels,
                        hovertemplate="%{customdata}<br>" + name + " %{y:.3f}<extra></extra>",
                    ),
                    row=r, col=1,
                )
        fig.update_yaxes(title_text="probability", row=r, col=1, rangemode="tozero")

    # ---- shared reference lines across every panel -------------------------
    for name in panels:
        r = row_of[name]
        for edge in bucket_edges:
            fig.add_vline(x=edge, line_width=1, line_dash="dot", line_color=_EDGE_COLOR, row=r, col=1)
        if show_market and view.market is not None and view.market.implied_mean_c is not None:
            fig.add_vline(
                x=view.market.implied_mean_c, line_width=2.5, line_color=_MARKET_COLOR,
                row=r, col=1,
            )
        if resolved_x is not None:
            fig.add_vline(x=resolved_x, line_width=3, line_color=_RESOLVED_COLOR, row=r, col=1)

    # bucket-label ticks on the bottom axis only
    bottom = len(panels)
    if centers:
        fig.update_xaxes(
            tickmode="array", tickvals=centers, ticktext=view.bucket_labels,
            tickangle=-45 if len(centers) > 7 else 0, row=bottom, col=1,
        )
    fig.update_xaxes(range=[x_min, x_max], title_text="°C (bucket centers)", row=bottom, col=1)

    fig.update_layout(
        barmode="overlay",
        height=720 if len(panels) == 2 else 460,
        margin=dict(l=55, r=20, t=70, b=70),
        legend=dict(
            orientation="h", yanchor="bottom", y=1.04, x=0,
            groupclick="toggleitem", font=dict(size=11),
        ),
        hovermode="x unified",
    )
    return fig
