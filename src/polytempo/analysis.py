"""Local analysis use case.

Connects bucket parsing, distribution math, edge calculation, and decision rules.
This module uses local inputs only; it does not fetch forecasts or markets.
"""

from __future__ import annotations

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
    CalibrationStatRow,
    read_calibration_stats_csv,
    select_best_model,
)
from polytempo.weather.schema import ForecastValues

MODEL_STRATEGY_ENSEMBLE_SPREAD = "ensemble_spread"
MODEL_STRATEGY_BEST_HISTORICAL = "best_historical"
MODEL_STRATEGIES: tuple[str, ...] = (
    MODEL_STRATEGY_ENSEMBLE_SPREAD,
    MODEL_STRATEGY_BEST_HISTORICAL,
)


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
    # Pre-bias-corrected mean for the calibrated path; ignored for ensemble path.
    calibrated_mu: float | None
    # Distribution sigma to use for the calibrated path; ignored otherwise.
    calibrated_sigma: float | None


def _resolve_strategy(
    *,
    forecast: ForecastValues,
    requested_strategy: str,
    station_id: str | None,
    current_lead_hours: float | None,
    calibration_stats_path: Path,
) -> _StrategyResolution:
    """Decide between best_historical and ensemble_spread, with reason on fallback.

    Returns a resolution describing which path the caller should run. When the
    requested strategy is ``best_historical`` and prerequisites cannot be met,
    the resolution drops back to ``ensemble_spread`` and records the reason.
    """
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

    if requested_strategy != MODEL_STRATEGY_BEST_HISTORICAL:
        raise ValueError(
            f"unknown model_strategy {requested_strategy!r}; "
            f"expected one of {MODEL_STRATEGIES}"
        )

    if not station_id:
        return _ensemble_fallback("missing_station_id")
    if current_lead_hours is None:
        return _ensemble_fallback("missing_lead_hours")
    if not forecast.models:
        return _ensemble_fallback("forecast_missing_model_identity")
    if len(forecast.models) != len(forecast.values_c):
        return _ensemble_fallback("forecast_model_value_length_mismatch")

    rows = read_calibration_stats_csv(calibration_stats_path)
    if not rows:
        return _ensemble_fallback("no_calibration_csv")

    selection = select_best_model(
        rows,
        station_id=station_id,
        available_models=list(forecast.models),
        current_lead_hours=current_lead_hours,
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
        strategy=MODEL_STRATEGY_BEST_HISTORICAL,
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
    """Run several strategies against the same model+market output.

    Builds the distribution once (via the same ``_resolve_strategy`` +
    ``build_calibrated_distribution`` / ``build_distribution`` paths as
    ``analyze_event``) and dispatches to each strategy. Returns a mapping
    from ``strategy.name`` to its ``AnalysisResult``. Strategy-level metadata
    (``model_strategy``, ``selected_model``, ``fallback_reason`` …) is the
    same across all strategies because they share the fit.
    """
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

    if resolution.strategy == MODEL_STRATEGY_BEST_HISTORICAL:
        assert resolution.calibrated_mu is not None
        assert resolution.calibrated_sigma is not None
        distribution, distribution_build = build_calibrated_distribution(
            mu=resolution.calibrated_mu,
            sigma=resolution.calibrated_sigma,
            source_values_c=list(calibrated.values_c),
        )
    else:
        distribution, distribution_build = build_distribution(
            calibrated.values_c,
            default_sigma_c=default_sigma_c,
            lead_hours=lead_hours,
        )

    probabilities = probabilities_for_buckets(distribution, buckets)
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
            )
            for probability, edge, decision in zip(
                probabilities,
                edges,
                decisions,
                strict=True,
            )
        ]
        results[strategy.name] = AnalysisResult(
            distribution_mean_c=distribution.mean_c,
            distribution_sigma_c=distribution.sigma_c,
            distribution_build=distribution_build,
            rows=rows,
            model_strategy=resolution.strategy,
            selected_model=resolution.selected_model,
            calibration_row=resolution.calibration_row,
            calibration_sigma_source=resolution.sigma_source,
            fallback_reason=resolution.fallback_reason,
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
    """Run analysis over already-fetched weather and market inputs.

    Pass ``strategy`` to plug in an alternative rule set. ``decision_config``
    is a shortcut for tweaking the default ArgmaxYesStrategy thresholds and
    cannot be combined with ``strategy``.

    ``model_strategy`` selects how the forecast distribution is built:

    - ``"ensemble_spread"`` (default): legacy mean + spread across models.
    - ``"best_historical"``: pick the model with the lowest empirical error
      sigma at the appropriate calibration lead row, bias-correct its
      prediction, and use that model's empirical sigma. Requires
      ``station_id``, a non-``None`` ``lead_hours``, ``forecast.models``, and
      a populated calibration stats CSV; otherwise falls back to
      ``ensemble_spread`` and records ``fallback_reason``.
    """
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

    if resolution.strategy == MODEL_STRATEGY_BEST_HISTORICAL:
        assert resolution.calibrated_mu is not None
        assert resolution.calibrated_sigma is not None
        distribution, distribution_build = build_calibrated_distribution(
            mu=resolution.calibrated_mu,
            sigma=resolution.calibrated_sigma,
            source_values_c=list(calibrated.values_c),
        )
    else:
        distribution, distribution_build = build_distribution(
            calibrated.values_c,
            default_sigma_c=default_sigma_c,
            lead_hours=lead_hours,
        )

    probabilities = probabilities_for_buckets(distribution, buckets)
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
        model_strategy=resolution.strategy,
        selected_model=resolution.selected_model,
        calibration_row=resolution.calibration_row,
        calibration_sigma_source=resolution.sigma_source,
        fallback_reason=resolution.fallback_reason,
    )
