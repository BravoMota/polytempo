"""Tests for the local analysis use case."""

import pytest

from polytempo.analysis import AnalysisInput, analyze
from polytempo.strategy.edge import MarketPrice


def test_analyze_returns_one_row_per_bucket() -> None:
    result = analyze(
        AnalysisInput(
            forecast_values_c=[24.0],
            bucket_labels=["23°C", "24°C", "25°C"],
            market_prices=[],
        )
    )

    assert len(result.rows) == 3


def test_analyze_preserves_bucket_order() -> None:
    labels = ["25°C", "23°C", "24°C"]
    result = analyze(
        AnalysisInput(
            forecast_values_c=[24.0],
            bucket_labels=labels,
            market_prices=[],
        )
    )

    assert [row.label for row in result.rows] == labels


def test_analyze_includes_distribution_mean_and_sigma() -> None:
    result = analyze(
        AnalysisInput(
            forecast_values_c=[23.0, 24.0, 25.0],
            bucket_labels=["24°C"],
            market_prices=[],
        )
    )

    assert result.distribution_mean_c == pytest.approx(24.0)
    assert result.distribution_sigma_c > 0.0


def test_analyze_includes_probability_for_each_bucket() -> None:
    result = analyze(
        AnalysisInput(
            forecast_values_c=[24.0],
            bucket_labels=["23°C", "24°C"],
            market_prices=[],
        )
    )

    assert all(0.0 <= row.probability <= 1.0 for row in result.rows)
    assert sum(row.probability for row in result.rows) == pytest.approx(1.0)


def test_analyze_includes_edge_yes_pp() -> None:
    result = analyze(
        AnalysisInput(
            forecast_values_c=[24.0],
            bucket_labels=["24°C"],
            market_prices=[
                MarketPrice(
                    label="24°C",
                    yes_bid=0.75,
                    yes_ask=0.80,
                    liquidity_usd=250.0,
                )
            ],
        )
    )

    assert result.rows[0].edge_yes_pp == pytest.approx(20.0)


def test_analyze_returns_buy_yes_when_edge_and_liquidity_pass() -> None:
    result = analyze(
        AnalysisInput(
            forecast_values_c=[24.0],
            bucket_labels=["24°C"],
            market_prices=[
                MarketPrice(
                    label="24°C",
                    yes_bid=0.75,
                    yes_ask=0.80,
                    liquidity_usd=250.0,
                )
            ],
        )
    )

    assert result.rows[0].action == "BUY_YES"
    assert result.rows[0].reason == "edge and liquidity rules passed"


def test_analyze_returns_skip_when_edge_below_threshold() -> None:
    result = analyze(
        AnalysisInput(
            forecast_values_c=[24.0],
            bucket_labels=["24°C"],
            market_prices=[
                MarketPrice(
                    label="24°C",
                    yes_bid=0.95,
                    yes_ask=0.96,
                    liquidity_usd=250.0,
                )
            ],
        )
    )

    assert result.rows[0].action == "SKIP"
    assert result.rows[0].reason == "edge below threshold"


def test_analyze_carries_high_spread_warning() -> None:
    result = analyze(
        AnalysisInput(
            forecast_values_c=[24.0],
            bucket_labels=["24°C"],
            market_prices=[
                MarketPrice(
                    label="24°C",
                    yes_bid=0.60,
                    yes_ask=0.80,
                    liquidity_usd=250.0,
                    spread=0.20,
                )
            ],
        )
    )

    assert result.rows[0].action == "BUY_YES"
    assert result.rows[0].warnings == ["high spread"]


def test_analyze_works_when_market_price_is_missing_for_bucket() -> None:
    result = analyze(
        AnalysisInput(
            forecast_values_c=[24.0],
            bucket_labels=["23°C", "24°C"],
            market_prices=[
                MarketPrice(
                    label="23°C",
                    yes_bid=0.10,
                    yes_ask=0.20,
                    liquidity_usd=250.0,
                )
            ],
        )
    )

    missing = result.rows[1]
    assert missing.label == "24°C"
    assert missing.yes_ask is None
    assert missing.edge_yes_pp is None
    assert missing.action == "SKIP"
    assert missing.reason == "missing edge"
