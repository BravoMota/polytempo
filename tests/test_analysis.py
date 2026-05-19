"""Tests for the local analysis use case."""

from datetime import date

import pytest

from polytempo.analysis import AnalysisInput, analyze, analyze_event
from polytempo.markets.polymarket import PolymarketBucket, PolymarketEvent
from polytempo.model.calibration import CalibrationRule
from polytempo.strategy.decision import DecisionConfig
from polytempo.strategy.edge import MarketPrice
from polytempo.weather.schema import ForecastValues


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


def test_analyze_event_returns_one_row_per_polymarket_bucket() -> None:
    result = analyze_event(_forecast([24.0]), _event(["23°C", "24°C", "25°C"]))

    assert len(result.rows) == 3


def test_analyze_event_preserves_polymarket_bucket_order() -> None:
    labels = ["25°C", "23°C", "24°C"]
    result = analyze_event(_forecast([24.0]), _event(labels))

    assert [row.label for row in result.rows] == labels


def test_analyze_event_uses_forecast_values_for_distribution() -> None:
    result = analyze_event(_forecast([23.0, 24.0, 25.0]), _event(["24°C"]))

    assert result.distribution_mean_c == pytest.approx(24.0)
    assert result.distribution_sigma_c > 0.0


def test_analyze_event_applies_calibration_before_distribution() -> None:
    forecast = _forecast([24.0, 26.0])
    event = _event(["24°C"])
    rule = CalibrationRule(source="open_meteo", station_id=None, bias_c=1.0)

    uncalibrated = analyze_event(forecast, event, calibration_rule=None)
    calibrated = analyze_event(forecast, event, calibration_rule=rule)

    assert uncalibrated.distribution_mean_c == pytest.approx(25.0)
    assert calibrated.distribution_mean_c == pytest.approx(24.0)


def test_analyze_event_none_calibration_rule_behaves_like_zero_bias() -> None:
    forecast = _forecast([24.0, 26.0])
    event = _event(["24°C"])
    zero_rule = CalibrationRule(source="open_meteo", station_id=None, bias_c=0.0)

    without_rule = analyze_event(forecast, event, calibration_rule=None)
    with_zero_rule = analyze_event(forecast, event, calibration_rule=zero_rule)

    assert with_zero_rule.distribution_mean_c == pytest.approx(
        without_rule.distribution_mean_c
    )
    assert with_zero_rule.distribution_sigma_c == pytest.approx(
        without_rule.distribution_sigma_c
    )


def test_analyze_event_includes_edge_yes_pp_for_each_row() -> None:
    result = analyze_event(
        _forecast([24.0]),
        _event(["24°C"], yes_ask=0.80, liquidity_usd=250.0),
    )

    assert result.rows[0].edge_yes_pp == pytest.approx(20.0)


def test_analyze_event_returns_buy_yes_when_edge_and_liquidity_pass() -> None:
    result = analyze_event(
        _forecast([24.0]),
        _event(["24°C"], yes_ask=0.80, liquidity_usd=250.0),
    )

    assert result.rows[0].action == "BUY_YES"
    assert result.rows[0].reason == "edge and liquidity rules passed"


def test_analyze_event_returns_skip_when_edge_below_threshold() -> None:
    result = analyze_event(
        _forecast([24.0]),
        _event(["24°C"], yes_ask=0.96, liquidity_usd=250.0),
    )

    assert result.rows[0].action == "SKIP"
    assert result.rows[0].reason == "edge below threshold"


def test_analyze_event_carries_spread_warning_from_decision_rules() -> None:
    result = analyze_event(
        _forecast([24.0]),
        _event(["24°C"], yes_ask=0.80, liquidity_usd=250.0, spread=0.20),
    )

    assert result.rows[0].action == "BUY_YES"
    assert result.rows[0].warnings == ["high spread"]


def test_analyze_event_accepts_custom_decision_config() -> None:
    result = analyze_event(
        _forecast([24.0]),
        _event(["24°C"], yes_ask=0.94, liquidity_usd=250.0),
        decision_config=DecisionConfig(min_edge_pp=5.0),
    )

    assert result.rows[0].action == "BUY_YES"


def test_analyze_event_does_not_mutate_original_forecast() -> None:
    forecast = _forecast([24.0, 26.0])
    original_values = forecast.values_c
    event = _event(["24°C"])
    rule = CalibrationRule(source="open_meteo", station_id=None, bias_c=1.0)

    analyze_event(forecast, event, calibration_rule=rule)

    assert forecast.values_c == [24.0, 26.0]
    assert forecast.values_c is original_values


def _forecast(values_c: list[float]) -> ForecastValues:
    return ForecastValues(
        source="open_meteo",
        latitude=40.4168,
        longitude=-3.7038,
        target_date=date(2026, 5, 14),
        values_c=values_c,
    )


def _event(
    labels: list[str],
    *,
    yes_ask: float | None = 0.90,
    liquidity_usd: float | None = 250.0,
    spread: float | None = None,
) -> PolymarketEvent:
    return PolymarketEvent(
        event_id="event-1",
        slug="temperature-event",
        title="Temperature Event",
        settlement_date=None,
        buckets=[
            PolymarketBucket(
                market_id=f"market-{i}",
                label=label,
                yes_bid=0.75,
                yes_ask=yes_ask,
                liquidity_usd=liquidity_usd,
                spread=spread,
                rules=None,
            )
            for i, label in enumerate(labels)
        ],
    )
