"""Smoke tests for polytempo.visualizer.chart."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from polytempo.analysis import AnalysisResult, AnalysisRow
from polytempo.markets.polymarket import PolymarketBucket, PolymarketEvent
from polytempo.model.distribution import DistributionBuildInfo, normal_pdf
from polytempo.visualizer.chart import (
    bucket_table_dataframe,
    bucket_table_has_replay_mismatch,
    build_analysis_chart,
    build_pivot_dataframe,
    build_pivot_heatmap,
    compute_market_implied_summary,
    pivot_click_to_wallet_date,
)
from polytempo.visualizer.trades import P_FROM_TRADES_COL
from polytempo.visualizer.styling import black_to_blue_bg, black_to_yellow_bg, cell_bg, style_bucket_table


def test_normal_pdf_peak_near_mean() -> None:
    assert normal_pdf(20.0, 20.0, 1.0) > normal_pdf(22.0, 20.0, 1.0)


def test_build_pivot_heatmap_smoke() -> None:
    df = pd.DataFrame(
        [
            {
                "profile_id": "w1",
                "settlement_date": date(2026, 6, 17),
                "pnl_pct": 2.5,
            },
            {
                "profile_id": "w1",
                "settlement_date": date(2026, 6, 18),
                "pnl_pct": -1.0,
            },
        ]
    )
    pivot_df, date_labels, vabs = build_pivot_dataframe(df)
    fig = build_pivot_heatmap(pivot_df, date_labels, vabs)
    assert fig.data[0].z is not None


def test_pivot_cell_mapping() -> None:
    pivot = pd.DataFrame(
        [
            {"wallet": "w1", "Σ%": 3.0, "2026-06-17": 2.5, "2026-06-18": -1.0},
        ]
    )
    dates = ["2026-06-17", "2026-06-18"]
    from polytempo.visualizer.chart import pivot_cell_to_wallet_date

    assert pivot_cell_to_wallet_date(0, "2026-06-17", pivot, dates) == (
        "w1",
        date(2026, 6, 17),
    )
    assert pivot_cell_to_wallet_date(0, "Σ%", pivot, dates) is None


def test_pivot_click_mapping() -> None:
    wallets = ["w1", "w2"]
    dates = ["2026-06-17", "2026-06-18"]
    mapped = pivot_click_to_wallet_date(
        {"y": "w1", "x": "2026-06-17"},
        wallets,
        dates,
    )
    assert mapped == ("w1", date(2026, 6, 17))
    assert pivot_click_to_wallet_date({"y": "w1", "x": "Σ%"}, wallets, dates) is None


def test_build_analysis_chart_smoke() -> None:
    build = DistributionBuildInfo(
        values_used_c=[24.0, 25.0],
        default_sigma_c=1.0,
        lead_hours=42.0,
        lead_hours_sigma_floor_c=1.2,
        ensemble_stdev_c=0.5,
        mean_c=24.5,
        sigma_c=1.3,
        method="lead_time_multi_quadrature",
    )
    analysis = AnalysisResult(
        distribution_mean_c=24.5,
        distribution_sigma_c=1.3,
        distribution_build=build,
        rows=[
            AnalysisRow(
                label="26°C",
                probability=0.4,
                yes_ask=0.35,
                edge_yes_pp=5.0,
                action="BUY_YES",
                reason="edge",
                confidence="high",
                warnings=[],
            )
        ],
        model_strategy="ensemble_spread",
    )
    event = PolymarketEvent(
        event_id="evt",
        slug="slug",
        title="title",
        settlement_date=date(2026, 6, 17),
        buckets=[
            PolymarketBucket(
                market_id="m1",
                label="26°C",
                yes_bid=0.33,
                yes_ask=0.35,
                liquidity_usd=10.0,
                spread=0.02,
                rules=None,
            )
        ],
    )
    bundle = build_analysis_chart(
        analysis,
        event,
        traded_bucket_labels={"26°C"},
        resolution_label="26°C",
    )
    assert len(bundle.figure.data) >= 3
    assert bundle.figure.layout.xaxis.tickmode == "array"
    assert bundle.figure.layout.xaxis.ticktext == ("26°C",)
    shapes = bundle.figure.layout.shapes or []
    assert len(shapes) >= 3  # resolution + model mean + market mean vlines
    model_bar = bundle.figure.data[1]
    assert model_bar.name == "model P"
    assert model_bar.marker.color[0] == "#ffcc00"
    assert bundle.market is not None
    assert bundle.market.mean_c == 26.0


def test_compute_market_implied_summary() -> None:
    summary = compute_market_implied_summary(
        ["25°C", "26°C", "27°C"],
        [0.2, 0.5, 0.3],
    )
    assert summary is not None
    assert summary.mean_c == 26.1
    assert summary.discrete_std_c == 0.7

    weighted = compute_market_implied_summary(
        ["25°C", "27°C"],
        [0.25, 0.75],
    )
    assert weighted is not None
    assert weighted.mean_c == 26.5
    assert weighted.discrete_std_c > 0


def test_bucket_table_dataframe() -> None:
    analysis = AnalysisResult(
        distribution_mean_c=24.5,
        distribution_sigma_c=1.3,
        distribution_build=DistributionBuildInfo(
            values_used_c=[24.0],
            default_sigma_c=1.0,
            lead_hours=42.0,
            lead_hours_sigma_floor_c=1.2,
            ensemble_stdev_c=0.5,
            mean_c=24.5,
            sigma_c=1.3,
            method="lead_time_multi_quadrature",
        ),
        rows=[
            AnalysisRow(
                label="26°C",
                probability=0.4,
                yes_ask=0.35,
                edge_yes_pp=5.0,
                action="BUY_YES",
                reason="edge",
                confidence="high",
                warnings=[],
            )
        ],
        model_strategy="ensemble_spread",
    )
    df = bucket_table_dataframe(analysis, trade_p_by_bucket={"26°C": 0.39})
    assert list(df.columns) == ["bucket", P_FROM_TRADES_COL, "P", "yes_ask", "edge"]
    assert df.iloc[0]["edge"] == pytest.approx(0.05)
    assert df.iloc[0]["yes_ask"] == 0.35
    assert df.iloc[0][P_FROM_TRADES_COL] == 0.39
    assert bucket_table_has_replay_mismatch(df)

    matched = bucket_table_dataframe(analysis, trade_p_by_bucket={"26°C": 0.4})
    assert not bucket_table_has_replay_mismatch(matched)

    styled = style_bucket_table(df)
    assert "rgb" in styled.to_html()


def test_black_to_yellow_and_blue_gradients() -> None:
    assert "255,255,0" in black_to_yellow_bg(1.0, 1.0)
    assert "0,0,255" in black_to_blue_bg(1.0, 1.0)
    assert "#000000" in black_to_yellow_bg(0.0, 1.0)
    assert "255,0,0" in cell_bg(-1.0, 1.0)
    assert "0,255,0" in cell_bg(1.0, 1.0)
