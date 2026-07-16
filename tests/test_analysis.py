"""Tests for the local analysis use case."""

from datetime import date
from pathlib import Path

import pytest

from polytempo.analysis import (
    MODEL_STRATEGY_BEST_HISTORICAL,
    MODEL_STRATEGY_ENSEMBLE_SPREAD,
    MODEL_STRATEGY_WEIGHTED_HISTORICAL_MARKET_SIGMA,
    MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED,
    AnalysisInput,
    analyze,
    analyze_event,
)
from polytempo.markets.polymarket import PolymarketBucket, PolymarketEvent
from polytempo.model.calibration import CalibrationRule
from polytempo.strategy.decision import DecisionConfig
from polytempo.strategy.edge import MarketPrice
from polytempo.visualizer.bucket_math import cap_sigma_to_market
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
    from polytempo.strategy import ArgmaxYesStrategy

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
        ),
        strategy=ArgmaxYesStrategy(config=DecisionConfig(min_edge_pp=7.0)),
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


def test_calibrate_forecast_preserves_init_lead_hours() -> None:
    from polytempo.model.calibration import calibrate_forecast

    forecast = ForecastValues(
        source="open_meteo",
        latitude=51.5,
        longitude=0.05,
        target_date=date(2026, 6, 10),
        values_c=[24.0, 25.0],
        models=["alpha", "beta"],
        init_lead_hours=[34.0, 28.0],
        model_run_init_utc=["2026-06-10T16:00:00Z", "2026-06-10T18:00:00Z"],
    )

    calibrated = calibrate_forecast(forecast, CalibrationRule(source="open_meteo", station_id=None))

    assert calibrated.init_lead_hours == [34.0, 28.0]
    assert calibrated.model_run_init_utc == [
        "2026-06-10T16:00:00Z",
        "2026-06-10T18:00:00Z",
    ]


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
        decision_config=DecisionConfig(min_edge_pp=7.0),
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


def test_analyze_event_accepts_custom_strategy() -> None:
    from polytempo.strategy import ArgmaxYesStrategy
    from polytempo.strategy.decision import DecisionConfig

    result = analyze_event(
        _forecast([24.0]),
        _event(["24°C"], yes_ask=0.94, liquidity_usd=250.0),
        strategy=ArgmaxYesStrategy(config=DecisionConfig(min_edge_pp=5.0)),
    )

    assert result.rows[0].action == "BUY_YES"


def test_analyze_event_rejects_strategy_and_decision_config_together() -> None:
    from polytempo.strategy import ArgmaxYesStrategy

    with pytest.raises(ValueError):
        analyze_event(
            _forecast([24.0]),
            _event(["24°C"]),
            strategy=ArgmaxYesStrategy(),
            decision_config=DecisionConfig(),
        )


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


def test_analyze_event_multi_runs_each_strategy_against_same_inputs() -> None:
    from polytempo.analysis import analyze_event_multi
    from polytempo.strategy import ArgmaxYesStrategy, DistArbStrategy

    strategies = [ArgmaxYesStrategy(), DistArbStrategy()]
    results = analyze_event_multi(
        _forecast([24.0]),
        _event(["24°C"], yes_ask=0.80, liquidity_usd=250.0),
        strategies=strategies,
    )

    assert set(results.keys()) == {"argmax_yes", "dist_arb"}
    for name in results:
        assert results[name].rows[0].action == "BUY_YES"


def test_analyze_event_multi_rejects_duplicate_names() -> None:
    from polytempo.analysis import analyze_event_multi
    from polytempo.strategy import ArgmaxYesStrategy

    with pytest.raises(ValueError):
        analyze_event_multi(
            _forecast([24.0]),
            _event(["24°C"]),
            strategies=[ArgmaxYesStrategy(), ArgmaxYesStrategy()],
        )


_CALIBRATION_HEADER = (
    "station_id,model,lead_hours,n_samples,bias_c,mae_c,rmse_c,error_std_c"
)


def _write_calibration_csv(path: Path, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([_CALIBRATION_HEADER, *rows]) + "\n", encoding="utf-8")


def test_analyze_event_default_strategy_is_ensemble_spread() -> None:
    result = analyze_event(_forecast([24.0, 25.0]), _event(["24°C"]))

    assert result.model_strategy == MODEL_STRATEGY_ENSEMBLE_SPREAD
    assert result.selected_model is None
    assert result.calibration_row is None
    assert result.fallback_reason is None


def test_analyze_event_best_historical_selects_lowest_error_std_model(tmp_path: Path) -> None:
    csv_path = tmp_path / "calibration_stats.csv"
    _write_calibration_csv(
        csv_path,
        [
            # alpha: high sigma; beta: low sigma + nonzero bias → mean = 25 - 0.5 = 24.5
            "EGLC,alpha,24,40,0.0,1.5,1.8,1.5",
            "EGLC,beta,24,40,0.5,0.9,1.0,0.8",
        ],
    )
    forecast = _forecast([23.0, 25.0], models=["alpha", "beta"])

    result = analyze_event(
        forecast,
        _event(["24°C"]),
        lead_hours=12.0,
        model_strategy=MODEL_STRATEGY_BEST_HISTORICAL,
        station_id="EGLC",
        calibration_stats_path=csv_path,
    )

    assert result.model_strategy == MODEL_STRATEGY_BEST_HISTORICAL
    assert result.selected_model == "beta"
    assert result.calibration_sigma_source == "error_std_c"
    assert result.fallback_reason is None
    assert result.distribution_mean_c == pytest.approx(24.5)
    assert result.distribution_sigma_c == pytest.approx(0.8)
    assert result.calibration_row is not None
    assert result.calibration_row.model == "beta"
    assert result.calibration_row.lead_hours == 24.0


def test_analyze_event_best_historical_falls_back_to_rmse_when_std_zero(tmp_path: Path) -> None:
    csv_path = tmp_path / "calibration_stats.csv"
    _write_calibration_csv(
        csv_path,
        [
            "EGLC,alpha,24,40,0.0,0.9,1.5,0.0",
            "EGLC,beta,24,40,0.2,0.6,0.7,0.0",
        ],
    )
    forecast = _forecast([23.0, 25.0], models=["alpha", "beta"])

    result = analyze_event(
        forecast,
        _event(["24°C"]),
        lead_hours=12.0,
        model_strategy=MODEL_STRATEGY_BEST_HISTORICAL,
        station_id="EGLC",
        calibration_stats_path=csv_path,
    )

    assert result.selected_model == "beta"
    assert result.calibration_sigma_source == "rmse_c"
    assert result.distribution_sigma_c == pytest.approx(0.7)
    assert result.distribution_mean_c == pytest.approx(25.0 - 0.2)


def test_analyze_event_best_historical_missing_csv_falls_back(tmp_path: Path) -> None:
    forecast = _forecast([24.0, 25.0], models=["alpha", "beta"])

    result = analyze_event(
        forecast,
        _event(["24°C"]),
        lead_hours=12.0,
        model_strategy=MODEL_STRATEGY_BEST_HISTORICAL,
        station_id="EGLC",
        calibration_stats_path=tmp_path / "does_not_exist.csv",
    )

    assert result.model_strategy == MODEL_STRATEGY_ENSEMBLE_SPREAD
    assert result.fallback_reason == "no_calibration_csv"
    assert result.selected_model is None


def test_analyze_event_best_historical_no_ceiling_row_falls_back(tmp_path: Path) -> None:
    csv_path = tmp_path / "calibration_stats.csv"
    _write_calibration_csv(
        csv_path,
        [
            "EGLC,alpha,6,40,0.0,1.0,1.0,1.0",
            "EGLC,beta,12,40,0.0,1.0,1.0,1.0",
        ],
    )
    forecast = _forecast([24.0, 25.0], models=["alpha", "beta"])

    result = analyze_event(
        forecast,
        _event(["24°C"]),
        lead_hours=48.0,
        model_strategy=MODEL_STRATEGY_BEST_HISTORICAL,
        station_id="EGLC",
        calibration_stats_path=csv_path,
    )

    assert result.model_strategy == MODEL_STRATEGY_ENSEMBLE_SPREAD
    assert result.fallback_reason == "no_ceiling_row_for_any_live_model"


def test_analyze_event_best_historical_missing_models_falls_back(tmp_path: Path) -> None:
    csv_path = tmp_path / "calibration_stats.csv"
    _write_calibration_csv(
        csv_path,
        ["EGLC,alpha,24,40,0.0,1.0,1.0,1.0"],
    )
    forecast = _forecast([24.0, 25.0])  # models=None

    result = analyze_event(
        forecast,
        _event(["24°C"]),
        lead_hours=12.0,
        model_strategy=MODEL_STRATEGY_BEST_HISTORICAL,
        station_id="EGLC",
        calibration_stats_path=csv_path,
    )

    assert result.model_strategy == MODEL_STRATEGY_ENSEMBLE_SPREAD
    assert result.fallback_reason == "forecast_missing_model_identity"


def test_analyze_event_best_historical_requires_station_id(tmp_path: Path) -> None:
    csv_path = tmp_path / "calibration_stats.csv"
    _write_calibration_csv(
        csv_path,
        ["EGLC,alpha,24,40,0.0,1.0,1.0,1.0"],
    )
    forecast = _forecast([24.0], models=["alpha"])

    result = analyze_event(
        forecast,
        _event(["24°C"]),
        lead_hours=12.0,
        model_strategy=MODEL_STRATEGY_BEST_HISTORICAL,
        station_id=None,
        calibration_stats_path=csv_path,
    )

    assert result.model_strategy == MODEL_STRATEGY_ENSEMBLE_SPREAD
    assert result.fallback_reason == "missing_station_id"


def test_analyze_event_best_historical_requires_lead_hours(tmp_path: Path) -> None:
    csv_path = tmp_path / "calibration_stats.csv"
    _write_calibration_csv(
        csv_path,
        ["EGLC,alpha,24,40,0.0,1.0,1.0,1.0"],
    )
    forecast = _forecast([24.0], models=["alpha"])

    result = analyze_event(
        forecast,
        _event(["24°C"]),
        lead_hours=None,
        model_strategy=MODEL_STRATEGY_BEST_HISTORICAL,
        station_id="EGLC",
        calibration_stats_path=csv_path,
    )

    assert result.model_strategy == MODEL_STRATEGY_ENSEMBLE_SPREAD
    assert result.fallback_reason == "missing_lead_hours"


def test_analyze_event_best_historical_uses_init_lead_hours(tmp_path: Path) -> None:
    csv_path = tmp_path / "calibration_stats.csv"
    _write_calibration_csv(
        csv_path,
        [
            "EGLC,alpha,30,40,0.0,1.0,1.0,1.0",
            "EGLC,alpha,34,40,0.0,1.0,1.0,2.0",
            "EGLC,beta,28,40,0.0,1.0,1.0,0.8",
            "EGLC,beta,30,40,0.0,1.0,1.0,1.5",
        ],
    )
    forecast = ForecastValues(
        source="open_meteo",
        latitude=51.5,
        longitude=0.05,
        target_date=date(2026, 6, 10),
        values_c=[23.0, 25.0],
        models=["alpha", "beta"],
        init_lead_hours=[34.0, 28.0],
    )

    result = analyze_event(
        forecast,
        _event(["24°C"]),
        lead_hours=30.0,
        model_strategy=MODEL_STRATEGY_BEST_HISTORICAL,
        station_id="EGLC",
        calibration_stats_path=csv_path,
    )

    assert result.model_strategy == MODEL_STRATEGY_BEST_HISTORICAL
    assert result.selected_model == "beta"
    assert result.calibration_row is not None
    assert result.calibration_row.lead_hours == 28.0


def test_analyze_event_best_historical_excludes_models_without_init_meta(
    tmp_path: Path,
) -> None:
    """Models without run_init must not win via wall-clock lead (unfair low σ)."""
    csv_path = tmp_path / "calibration_stats.csv"
    _write_calibration_csv(
        csv_path,
        [
            "EGLC,alpha,24,40,0.0,1.0,1.0,1.0",
            "EGLC,beta,12,40,0.0,1.0,1.0,0.8",
        ],
    )
    forecast = ForecastValues(
        source="open_meteo",
        latitude=51.5,
        longitude=0.05,
        target_date=date(2026, 6, 14),
        values_c=[23.0, 25.0],
        models=["alpha", "beta"],
        init_lead_hours=[24.0, 11.0],
        model_run_init_utc=["2026-06-14T00:00:00Z", ""],
    )

    result = analyze_event(
        forecast,
        _event(["24°C"]),
        lead_hours=11.0,
        model_strategy=MODEL_STRATEGY_BEST_HISTORICAL,
        station_id="EGLC",
        calibration_stats_path=csv_path,
    )

    assert result.model_strategy == MODEL_STRATEGY_BEST_HISTORICAL
    assert result.selected_model == "alpha"
    assert result.calibration_row is not None
    assert result.calibration_row.lead_hours == 24.0


def test_analyze_event_best_historical_no_models_with_init_meta_falls_back(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "calibration_stats.csv"
    _write_calibration_csv(
        csv_path,
        ["EGLC,beta,12,40,0.0,1.0,1.0,0.8"],
    )
    forecast = ForecastValues(
        source="open_meteo",
        latitude=51.5,
        longitude=0.05,
        target_date=date(2026, 6, 14),
        values_c=[25.0],
        models=["beta"],
        init_lead_hours=[11.0],
        model_run_init_utc=[""],
    )

    result = analyze_event(
        forecast,
        _event(["24°C"]),
        lead_hours=11.0,
        model_strategy=MODEL_STRATEGY_BEST_HISTORICAL,
        station_id="EGLC",
        calibration_stats_path=csv_path,
    )

    assert result.model_strategy == MODEL_STRATEGY_ENSEMBLE_SPREAD
    assert result.fallback_reason == "no_ceiling_row_for_any_live_model"


def test_analyze_event_best_historical_init_lead_length_mismatch_falls_back(
    tmp_path: Path,
) -> None:
    from unittest.mock import MagicMock

    from polytempo.analysis import MODEL_STRATEGY_BEST_HISTORICAL, _resolve_strategy

    csv_path = tmp_path / "calibration_stats.csv"
    _write_calibration_csv(
        csv_path,
        ["EGLC,alpha,24,40,0.0,1.0,1.0,1.0"],
    )
    forecast = MagicMock()
    forecast.models = ["alpha", "beta"]
    forecast.values_c = [24.0, 25.0]
    forecast.init_lead_hours = [34.0]

    resolution = _resolve_strategy(
        forecast=forecast,
        requested_strategy=MODEL_STRATEGY_BEST_HISTORICAL,
        station_id="EGLC",
        current_lead_hours=12.0,
        calibration_stats_path=csv_path,
    )

    assert resolution.strategy == MODEL_STRATEGY_ENSEMBLE_SPREAD
    assert resolution.fallback_reason == "forecast_init_lead_length_mismatch"


def test_analyze_event_rejects_unknown_model_strategy() -> None:
    with pytest.raises(ValueError, match="model_strategy"):
        analyze_event(
            _forecast([24.0]),
            _event(["24°C"]),
            model_strategy="some_other_strategy",
            station_id="EGLC",
            lead_hours=12.0,
        )


def _forecast(values_c: list[float], models: list[str] | None = None) -> ForecastValues:
    return ForecastValues(
        source="open_meteo",
        latitude=40.4168,
        longitude=-3.7038,
        target_date=date(2026, 5, 14),
        values_c=values_c,
        models=models,
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


def test_analyze_event_weighted_historical_updated_blends_models(tmp_path: Path) -> None:
    csv_path = tmp_path / "calibration_stats_updated.csv"
    _write_calibration_csv(
        csv_path,
        [
            "EGLC,alpha,24,40,0.0,1.5,1.8,1.0",
            "EGLC,beta,24,40,0.0,0.9,1.0,1.2",
            "EGLC,gamma,24,40,0.0,1.0,1.2,1.5",
        ],
    )
    forecast = _forecast([28.0, 28.2, 27.9], models=["alpha", "beta", "gamma"])

    result = analyze_event(
        forecast,
        _event(["28°C"]),
        lead_hours=12.0,
        model_strategy=MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED,
        station_id="EGLC",
        calibration_stats_path=csv_path,
    )

    assert result.model_strategy == MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED
    assert result.fallback_reason is None
    assert result.distribution_mean_c == pytest.approx(28.05, abs=0.02)
    assert result.distribution_sigma_c == pytest.approx(1.13, abs=0.02)
    assert result.weighted_contributions is not None
    assert len(result.weighted_contributions) == 3
    assert result.distribution_params is not None
    assert result.distribution_params["method"] == "weighted_calibrated_mixture_p2.0"


def test_analyze_event_weighted_historical_updated_failure_stays_whu(tmp_path: Path) -> None:
    forecast = _forecast([28.0], models=["alpha"])

    result = analyze_event(
        forecast,
        _event(["28°C"]),
        lead_hours=12.0,
        model_strategy=MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED,
        station_id="EGLC",
        calibration_stats_path=tmp_path / "missing.csv",
    )

    assert result.model_strategy == MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED
    assert result.fallback_reason == "no_calibration_csv"
    assert result.rows == []
    assert result.distribution_params is not None
    assert result.distribution_params["error"] == "no_calibration_csv"


def test_cap_sigma_to_market_when_means_agree() -> None:
    labels = ["26°C", "27°C", "28°C", "29°C", "30°C"]
    asks = [0.03, 0.07, 0.70, 0.15, 0.05]
    capped, audit = cap_sigma_to_market(
        28.05,
        1.13,
        labels=labels,
        yes_asks=asks,
    )
    assert audit["applied"] is True
    assert audit["reason"] == "capped_to_market"
    assert capped < 1.13


def test_cap_sigma_to_market_skips_when_means_diverge() -> None:
    labels = ["26°C", "27°C", "28°C", "29°C", "30°C"]
    asks = [0.03, 0.07, 0.10, 0.15, 0.65]
    capped, audit = cap_sigma_to_market(
        28.05,
        1.13,
        labels=labels,
        yes_asks=asks,
        max_mean_delta_c=1.0,
    )
    assert capped == 1.13
    assert audit["applied"] is False
    assert audit["reason"] == "means_diverge"


def _event_with_asks(
    labels: list[str],
    asks: list[float | None],
    *,
    liquidity_usd: float | None = 250.0,
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
                yes_bid=max((ask or 0.0) - 0.05, 0.0),
                yes_ask=ask,
                liquidity_usd=liquidity_usd,
                spread=0.05,
                rules=None,
            )
            for i, (label, ask) in enumerate(zip(labels, asks, strict=True))
        ],
    )


def test_analyze_event_weighted_historical_market_sigma_caps_sigma(tmp_path: Path) -> None:
    csv_path = tmp_path / "calibration_stats_updated.csv"
    _write_calibration_csv(
        csv_path,
        [
            "EGLC,alpha,24,40,0.0,1.5,1.8,1.0",
            "EGLC,beta,24,40,0.0,0.9,1.0,1.2",
            "EGLC,gamma,24,40,0.0,1.0,1.2,1.5",
        ],
    )
    forecast = _forecast([28.0, 28.2, 27.9], models=["alpha", "beta", "gamma"])
    labels = ["26°C", "27°C", "28°C", "29°C", "30°C"]
    asks = [0.03, 0.07, 0.70, 0.15, 0.05]
    event = _event_with_asks(labels, asks)

    whu = analyze_event(
        forecast,
        event,
        lead_hours=12.0,
        model_strategy=MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED,
        station_id="EGLC",
        calibration_stats_path=csv_path,
    )
    whums = analyze_event(
        forecast,
        event,
        lead_hours=12.0,
        model_strategy=MODEL_STRATEGY_WEIGHTED_HISTORICAL_MARKET_SIGMA,
        station_id="EGLC",
        calibration_stats_path=csv_path,
    )

    assert whu.distribution_sigma_c == pytest.approx(1.13, abs=0.02)
    assert whums.model_strategy == MODEL_STRATEGY_WEIGHTED_HISTORICAL_MARKET_SIGMA
    assert whums.distribution_mean_c == pytest.approx(whu.distribution_mean_c, abs=0.01)
    assert whums.distribution_sigma_c < whu.distribution_sigma_c
    assert whums.distribution_build.method == "weighted_historical_market_sigma_cap"
    assert whums.distribution_params is not None
    cap = whums.distribution_params["market_sigma_cap"]
    assert isinstance(cap, dict)
    assert cap["applied"] is True
    assert cap["reason"] == "capped_to_market"


def test_analyze_event_weighted_historical_market_sigma_failure_stays_whums(
    tmp_path: Path,
) -> None:
    forecast = _forecast([28.0], models=["alpha"])

    result = analyze_event(
        forecast,
        _event(["28°C"]),
        lead_hours=12.0,
        model_strategy=MODEL_STRATEGY_WEIGHTED_HISTORICAL_MARKET_SIGMA,
        station_id="EGLC",
        calibration_stats_path=tmp_path / "missing.csv",
    )

    assert result.model_strategy == MODEL_STRATEGY_WEIGHTED_HISTORICAL_MARKET_SIGMA
    assert result.fallback_reason == "no_calibration_csv"
    assert result.rows == []


def test_analyze_event_conditions_probs_on_observed_running_max() -> None:
    labels = ["19°C", "20°C", "21°C", "22°C", "23°C"]
    base = _forecast([21.0])
    with_floor = ForecastValues(
        source=base.source,
        latitude=base.latitude,
        longitude=base.longitude,
        target_date=base.target_date,
        values_c=list(base.values_c),
        models=base.models,
        observed_running_max_c=21.0,
    )
    without = analyze_event(base, _event(labels, yes_ask=0.20))
    with_cond = analyze_event(with_floor, _event(labels, yes_ask=0.20))

    without_by = {row.label: row.probability for row in without.rows}
    with_by = {row.label: row.probability for row in with_cond.rows}
    assert with_by["19°C"] == 0.0
    assert with_by["20°C"] == 0.0
    assert with_by["21°C"] > without_by["21°C"]
    assert sum(with_by.values()) == pytest.approx(1.0)
    assert with_cond.distribution_params is not None
    floor = with_cond.distribution_params["observed_floor"]
    assert floor["applied"] is True
    assert floor["policy"] == "renormalize"
    assert floor["floor_c"] == pytest.approx(21.0)
    assert without.distribution_params is None or "observed_floor" not in (
        without.distribution_params or {}
    )
