"""Local analysis use case.

Connects bucket parsing, distribution math, edge calculation, and decision rules.
This module uses local inputs only; it does not fetch forecasts or markets.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

from polytempo.markets.buckets import parse_temperature_bucket
from polytempo.markets.polymarket import PolymarketEvent, to_market_prices
from polytempo.model.calibration import CalibrationRule, calibrate_forecast
from polytempo.model.distribution import (
    DistributionBuildInfo,
    build_calibrated_distribution,
    build_distribution,
    probabilities_for_buckets,
)
from polytempo.strategy.argmax_yes import ArgmaxYesStrategy
from polytempo.strategy.base import Strategy
from polytempo.strategy.decision import DecisionConfig
from polytempo.strategy.edge import MarketPrice, ProbabilityQuote, calculate_bucket_edges
from polytempo.weather.calibration_stats_csv import (
    DEFAULT_CALIBRATION_STATS_CSV_PATH,
    WEIGHTED_CALIBRATION_METHOD,
    CalibrationStatRow,
    WeightedModelContribution,
    build_weighted_distribution_params,
    read_calibration_stats_csv,
    select_best_model,
    select_weighted_models,
    verified_init_lead_hours_by_model,
)
from polytempo.weather.schema import ForecastValues

logger = logging.getLogger(__name__)

MODEL_STRATEGY_ENSEMBLE_SPREAD = "ensemble_spread"
MODEL_STRATEGY_BEST_HISTORICAL = "best_historical"
MODEL_STRATEGY_BEST_HISTORICAL_UPDATED = "best_historical_updated"
MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED = "weighted_historical_updated"
MODEL_STRATEGIES: tuple[str, ...] = (
    MODEL_STRATEGY_ENSEMBLE_SPREAD,
    MODEL_STRATEGY_BEST_HISTORICAL,
    MODEL_STRATEGY_BEST_HISTORICAL_UPDATED,
    MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED,
)

_BEST_HISTORICAL_STRATEGIES = frozenset(
    {
        MODEL_STRATEGY_BEST_HISTORICAL,
        MODEL_STRATEGY_BEST_HISTORICAL_UPDATED,
    }
)
_CALIBRATED_STRATEGIES = _BEST_HISTORICAL_STRATEGIES | {
    MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED,
}
_SKIP_ON_FALLBACK_STRATEGIES = _CALIBRATED_STRATEGIES


@dataclass(frozen=True)
class AnalysisInput:
    """Inputs for one local max-temperature market analysis."""

    forecast_values_c: list[float]
    bucket_labels: list[str]
    market_prices: list[MarketPrice]


@dataclass(frozen=True)
class AnalysisRow:
    """Merged per-bucket probability, edge, and decision output."""

    label: str
    probability: float
    yes_ask: float | None
    edge_yes_pp: float | None
    action: str
    reason: str
    confidence: str
    warnings: list[str]
    side: str = "YES"
    yes_bid: float | None = None
    edge_no_pp: float | None = None
    stake_usd: float | None = None
    stake_fraction: float | None = None


@dataclass(frozen=True)
class AnalysisResult:
    """Complete local analysis result."""

    distribution_mean_c: float
    distribution_sigma_c: float
    distribution_build: DistributionBuildInfo
    rows: list[AnalysisRow]
    model_strategy: str = MODEL_STRATEGY_ENSEMBLE_SPREAD
    selected_model: str | None = None
    calibration_row: CalibrationStatRow | None = None
    calibration_sigma_source: str | None = None
    fallback_reason: str | None = None
    weighted_contributions: tuple[WeightedModelContribution, ...] | None = None
    distribution_params: dict[str, object] | None = None


def analyze(
    input_data: AnalysisInput,
    strategy: Strategy | None = None,
) -> AnalysisResult:
    """Run the local analysis pipeline over already-provided inputs."""
    strategy = strategy if strategy is not None else ArgmaxYesStrategy()
    buckets = [parse_temperature_bucket(label) for label in input_data.bucket_labels]
    distribution, distribution_build = build_distribution(input_data.forecast_values_c)
    probabilities = probabilities_for_buckets(distribution, buckets)
    quotes = [
        ProbabilityQuote(label=p.label, probability=p.probability)
        for p in probabilities
    ]
    edges = calculate_bucket_edges(quotes, input_data.market_prices)
    decisions = strategy.decide(edges)

    rows = [
        AnalysisRow(
            label=probability.label,
            probability=probability.probability,
            yes_ask=edge.yes_ask,
            edge_yes_pp=decision.edge_yes_pp,
            action=decision.action,
            reason=decision.reason,
            confidence=decision.confidence,
            warnings=list(decision.warnings),
            side=decision.side,
            yes_bid=edge.yes_bid,
            edge_no_pp=edge.edge_no_pp,
            stake_usd=decision.stake_usd,
            stake_fraction=decision.stake_fraction,
        )
        for probability, edge, decision in zip(
            probabilities,
            edges,
            decisions,
            strict=True,
        )
    ]

    return AnalysisResult(
        distribution_mean_c=distribution.mean_c,
        distribution_sigma_c=distribution.sigma_c,
        distribution_build=distribution_build,
        rows=rows,
    )


@dataclass(frozen=True)
class _StrategyResolution:
    """Internal: which path analyze_event should take after strategy selection."""

    strategy: str
    selected_model: str | None
    calibration_row: CalibrationStatRow | None
    sigma_source: str | None
    fallback_reason: str | None
    calibrated_mu: float | None
    calibrated_sigma: float | None
    weighted_contributions: tuple[WeightedModelContribution, ...] | None = None
    distribution_params: dict[str, object] | None = None


def _predicted_tmax_by_model(forecast: ForecastValues) -> dict[str, float]:
    if not forecast.models:
        return {}
    return {
        model: forecast.values_c[index]
        for index, model in enumerate(forecast.models)
        if index < len(forecast.values_c)
    }


def _failure_distribution_build(*, method: str) -> DistributionBuildInfo:
    return DistributionBuildInfo(
        values_used_c=[],
        default_sigma_c=float("nan"),
        lead_hours=None,
        lead_hours_sigma_floor_c=None,
        ensemble_stdev_c=None,
        mean_c=float("nan"),
        sigma_c=float("nan"),
        method=method,
    )


def _whu_failure(
    reason: str,
    *,
    distribution_params: dict[str, object] | None = None,
) -> _StrategyResolution:
    logger.warning("weighted_historical_updated failed: %s", reason)
    params = distribution_params if distribution_params is not None else {"error": reason}
    return _StrategyResolution(
        strategy=MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED,
        selected_model=None,
        calibration_row=None,
        sigma_source=None,
        fallback_reason=reason,
        calibrated_mu=None,
        calibrated_sigma=None,
        distribution_params=params,
    )


def _validate_calibration_prerequisites(
    *,
    forecast: ForecastValues,
    station_id: str | None,
    current_lead_hours: float | None,
) -> tuple[str | None, dict[str, float] | None]:
    """Return ``(fallback_reason, init_lead_hours_by_model)`` when invalid."""
    if not station_id:
        return "missing_station_id", None
    if current_lead_hours is None:
        return "missing_lead_hours", None
    if not forecast.models:
        return "forecast_missing_model_identity", None
    if len(forecast.models) != len(forecast.values_c):
        return "forecast_model_value_length_mismatch", None

    init_lead_hours_by_model: dict[str, float] | None = None
    if forecast.init_lead_hours is not None:
        if len(forecast.init_lead_hours) != len(forecast.models):
            return "forecast_init_lead_length_mismatch", None
        init_lead_hours_by_model = verified_init_lead_hours_by_model(forecast)
        if init_lead_hours_by_model is None:
            return "forecast_init_lead_length_mismatch", None
    return None, init_lead_hours_by_model


def _resolve_strategy(
    *,
    forecast: ForecastValues,
    requested_strategy: str,
    station_id: str | None,
    current_lead_hours: float | None,
    calibration_stats_path: Path,
) -> _StrategyResolution:
    """Decide distribution parameters for the requested model strategy."""
    if requested_strategy == MODEL_STRATEGY_ENSEMBLE_SPREAD:
        return _StrategyResolution(
            strategy=MODEL_STRATEGY_ENSEMBLE_SPREAD,
            selected_model=None,
            calibration_row=None,
            sigma_source=None,
            fallback_reason=None,
            calibrated_mu=None,
            calibrated_sigma=None,
        )

    if requested_strategy == MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED:
        prereq_reason, init_lead_hours_by_model = _validate_calibration_prerequisites(
            forecast=forecast,
            station_id=station_id,
            current_lead_hours=current_lead_hours,
        )
        if prereq_reason is not None:
            return _whu_failure(prereq_reason)

        rows = read_calibration_stats_csv(calibration_stats_path)
        if not rows:
            return _whu_failure("no_calibration_csv")

        attempt = select_weighted_models(
            rows,
            station_id=station_id,  # type: ignore[arg-type]
            available_models=list(forecast.models),
            current_lead_hours=current_lead_hours,  # type: ignore[arg-type]
            predicted_tmax_by_model=_predicted_tmax_by_model(forecast),
            init_lead_hours_by_model=init_lead_hours_by_model,
        )
        if attempt.result is None:
            return _whu_failure(
                "no_eligible_models",
                distribution_params={
                    "error": "no_eligible_models",
                    "excluded_models": list(attempt.excluded_models),
                },
            )

        result = attempt.result
        distribution_params = build_weighted_distribution_params(
            result,
            excluded_models=attempt.excluded_models,
        )
        top = max(result.contributions, key=lambda c: c.weight)
        return _StrategyResolution(
            strategy=MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED,
            selected_model=top.model,
            calibration_row=top.row,
            sigma_source="error_std_c",
            fallback_reason=None,
            calibrated_mu=result.mean_c,
            calibrated_sigma=result.sigma_c,
            weighted_contributions=result.contributions,
            distribution_params=distribution_params,
        )

    if requested_strategy not in _BEST_HISTORICAL_STRATEGIES:
        raise ValueError(
            f"unknown model_strategy {requested_strategy!r}; "
            f"expected one of {MODEL_STRATEGIES}"
        )

    prereq_reason, init_lead_hours_by_model = _validate_calibration_prerequisites(
        forecast=forecast,
        station_id=station_id,
        current_lead_hours=current_lead_hours,
    )
    if prereq_reason is not None:
        return _ensemble_fallback(prereq_reason)

    rows = read_calibration_stats_csv(calibration_stats_path)
    if not rows:
        return _ensemble_fallback("no_calibration_csv")

    selection = select_best_model(
        rows,
        station_id=station_id,  # type: ignore[arg-type]
        available_models=list(forecast.models),
        current_lead_hours=current_lead_hours,  # type: ignore[arg-type]
        init_lead_hours_by_model=init_lead_hours_by_model,
    )
    if selection is None:
        return _ensemble_fallback("no_ceiling_row_for_any_live_model")

    row, sigma_source = selection
    sigma = row.error_std_c if sigma_source == "error_std_c" else row.rmse_c

    try:
        index = forecast.models.index(row.model)
    except ValueError:
        return _ensemble_fallback("selected_model_not_in_live_forecast")

    predicted = forecast.values_c[index]
    mu = predicted - row.bias_c

    return _StrategyResolution(
        strategy=requested_strategy,
        selected_model=row.model,
        calibration_row=row,
        sigma_source=sigma_source,
        fallback_reason=None,
        calibrated_mu=mu,
        calibrated_sigma=sigma,
    )


def _ensemble_fallback(reason: str) -> _StrategyResolution:
    return _StrategyResolution(
        strategy=MODEL_STRATEGY_ENSEMBLE_SPREAD,
        selected_model=None,
        calibration_row=None,
        sigma_source=None,
        fallback_reason=reason,
        calibrated_mu=None,
        calibrated_sigma=None,
    )


def _analysis_result_from_resolution(
    resolution: _StrategyResolution,
    *,
    rows: list[AnalysisRow],
    distribution_mean_c: float,
    distribution_sigma_c: float,
    distribution_build: DistributionBuildInfo,
) -> AnalysisResult:
    return AnalysisResult(
        distribution_mean_c=distribution_mean_c,
        distribution_sigma_c=distribution_sigma_c,
        distribution_build=distribution_build,
        rows=rows,
        model_strategy=resolution.strategy,
        selected_model=resolution.selected_model,
        calibration_row=resolution.calibration_row,
        calibration_sigma_source=resolution.sigma_source,
        fallback_reason=resolution.fallback_reason,
        weighted_contributions=resolution.weighted_contributions,
        distribution_params=resolution.distribution_params,
    )


def _build_distribution_from_resolution(
    resolution: _StrategyResolution,
    *,
    calibrated: ForecastValues,
    default_sigma_c: float,
    lead_hours: float | None,
) -> tuple[float, float, DistributionBuildInfo] | None:
    """Return distribution triple, or ``None`` when WHU failed without fallback."""
    if (
        resolution.strategy == MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED
        and resolution.fallback_reason is not None
    ):
        return None

    if resolution.strategy in _CALIBRATED_STRATEGIES:
        assert resolution.calibrated_mu is not None
        assert resolution.calibrated_sigma is not None
        source_values = (
            [c.corrected_mu_c for c in resolution.weighted_contributions]
            if resolution.weighted_contributions is not None
            else list(calibrated.values_c)
        )
        method = (
            WEIGHTED_CALIBRATION_METHOD
            if resolution.strategy == MODEL_STRATEGY_WEIGHTED_HISTORICAL_UPDATED
            else "calibrated_single_model"
        )
        distribution, distribution_build = build_calibrated_distribution(
            mu=resolution.calibrated_mu,
            sigma=resolution.calibrated_sigma,
            source_values_c=source_values,
            method=method,
        )
        return distribution.mean_c, distribution.sigma_c, distribution_build

    distribution, distribution_build = build_distribution(
        calibrated.values_c,
        default_sigma_c=default_sigma_c,
        lead_hours=lead_hours,
    )
    return distribution.mean_c, distribution.sigma_c, distribution_build


def analyze_event_multi(
    forecast: ForecastValues,
    event: PolymarketEvent,
    strategies: list[Strategy],
    calibration_rule: CalibrationRule | None = None,
    lead_hours: float | None = None,
    model_strategy: str = MODEL_STRATEGY_ENSEMBLE_SPREAD,
    station_id: str | None = None,
    default_sigma_c: float = 1.0,
    calibration_stats_path: Path = DEFAULT_CALIBRATION_STATS_CSV_PATH,
) -> dict[str, AnalysisResult]:
    """Run several strategies against the same model+market output."""
    if not strategies:
        raise ValueError("strategies must not be empty")
    names = [s.name for s in strategies]
    if len(set(names)) != len(names):
        raise ValueError(f"strategy names must be unique, got {names!r}")

    market_prices = to_market_prices(event)
    bucket_labels = [bucket.label for bucket in event.buckets]
    calibrated = calibrate_forecast(forecast, calibration_rule)
    buckets = [parse_temperature_bucket(label) for label in bucket_labels]

    resolution = _resolve_strategy(
        forecast=calibrated,
        requested_strategy=model_strategy,
        station_id=station_id,
        current_lead_hours=lead_hours,
        calibration_stats_path=calibration_stats_path,
    )

    built = _build_distribution_from_resolution(
        resolution,
        calibrated=calibrated,
        default_sigma_c=default_sigma_c,
        lead_hours=lead_hours,
    )
    if built is None:
        failure_build = _failure_distribution_build(method="weighted_historical_failed")
        return {
            strategy.name: _analysis_result_from_resolution(
                resolution,
                rows=[],
                distribution_mean_c=math.nan,
                distribution_sigma_c=math.nan,
                distribution_build=failure_build,
            )
            for strategy in strategies
        }

    distribution_mean_c, distribution_sigma_c, distribution_build = built
    from polytempo.model.distribution import ForecastDistribution

    dist = ForecastDistribution(
        mean_c=distribution_mean_c,
        sigma_c=distribution_sigma_c,
        source_values_c=distribution_build.values_used_c,
    )
    probabilities = probabilities_for_buckets(dist, buckets)
    quotes = [
        ProbabilityQuote(label=p.label, probability=p.probability)
        for p in probabilities
    ]
    edges = calculate_bucket_edges(quotes, market_prices)

    results: dict[str, AnalysisResult] = {}
    for strategy in strategies:
        decisions = strategy.decide(edges)
        rows = [
            AnalysisRow(
                label=probability.label,
                probability=probability.probability,
                yes_ask=edge.yes_ask,
                edge_yes_pp=decision.edge_yes_pp,
                action=decision.action,
                reason=decision.reason,
                confidence=decision.confidence,
                warnings=list(decision.warnings),
                side=decision.side,
                yes_bid=edge.yes_bid,
                edge_no_pp=edge.edge_no_pp,
                stake_usd=decision.stake_usd,
                stake_fraction=decision.stake_fraction,
            )
            for probability, edge, decision in zip(
                probabilities,
                edges,
                decisions,
                strict=True,
            )
        ]
        results[strategy.name] = _analysis_result_from_resolution(
            resolution,
            rows=rows,
            distribution_mean_c=distribution_mean_c,
            distribution_sigma_c=distribution_sigma_c,
            distribution_build=distribution_build,
        )
    return results


def analyze_event(
    forecast: ForecastValues,
    event: PolymarketEvent,
    calibration_rule: CalibrationRule | None = None,
    decision_config: DecisionConfig | None = None,
    strategy: Strategy | None = None,
    lead_hours: float | None = None,
    default_sigma_c: float = 1.0,
    model_strategy: str = MODEL_STRATEGY_ENSEMBLE_SPREAD,
    station_id: str | None = None,
    calibration_stats_path: Path = DEFAULT_CALIBRATION_STATS_CSV_PATH,
) -> AnalysisResult:
    """Run analysis over already-fetched weather and market inputs."""
    if strategy is not None and decision_config is not None:
        raise ValueError("pass either strategy or decision_config, not both")
    if strategy is None:
        strategy = ArgmaxYesStrategy(config=decision_config or DecisionConfig())

    market_prices = to_market_prices(event)
    bucket_labels = [bucket.label for bucket in event.buckets]
    calibrated = calibrate_forecast(forecast, calibration_rule)
    buckets = [parse_temperature_bucket(label) for label in bucket_labels]

    resolution = _resolve_strategy(
        forecast=calibrated,
        requested_strategy=model_strategy,
        station_id=station_id,
        current_lead_hours=lead_hours,
        calibration_stats_path=calibration_stats_path,
    )

    built = _build_distribution_from_resolution(
        resolution,
        calibrated=calibrated,
        default_sigma_c=default_sigma_c,
        lead_hours=lead_hours,
    )
    if built is None:
        return _analysis_result_from_resolution(
            resolution,
            rows=[],
            distribution_mean_c=math.nan,
            distribution_sigma_c=math.nan,
            distribution_build=_failure_distribution_build(method="weighted_historical_failed"),
        )

    distribution_mean_c, distribution_sigma_c, distribution_build = built
    from polytempo.model.distribution import ForecastDistribution

    dist = ForecastDistribution(
        mean_c=distribution_mean_c,
        sigma_c=distribution_sigma_c,
        source_values_c=distribution_build.values_used_c,
    )
    probabilities = probabilities_for_buckets(dist, buckets)
    quotes = [
        ProbabilityQuote(label=p.label, probability=p.probability)
        for p in probabilities
    ]
    edges = calculate_bucket_edges(quotes, market_prices)
    decisions = strategy.decide(edges)

    rows = [
        AnalysisRow(
            label=probability.label,
            probability=probability.probability,
            yes_ask=edge.yes_ask,
            edge_yes_pp=decision.edge_yes_pp,
            action=decision.action,
            reason=decision.reason,
            confidence=decision.confidence,
            warnings=list(decision.warnings),
            side=decision.side,
            yes_bid=edge.yes_bid,
            edge_no_pp=edge.edge_no_pp,
            stake_usd=decision.stake_usd,
            stake_fraction=decision.stake_fraction,
        )
        for probability, edge, decision in zip(
            probabilities,
            edges,
            decisions,
            strict=True,
        )
    ]

    return _analysis_result_from_resolution(
        resolution,
        rows=rows,
        distribution_mean_c=distribution_mean_c,
        distribution_sigma_c=distribution_sigma_c,
        distribution_build=distribution_build,
    )
