"""Local analysis use case.

Connects bucket parsing, distribution math, edge calculation, and decision rules.
This module uses local inputs only; it does not fetch forecasts or markets.
"""

from __future__ import annotations

from dataclasses import dataclass

from polytempo.markets.buckets import parse_temperature_bucket
from polytempo.markets.polymarket import PolymarketEvent, to_market_prices
from polytempo.model.calibration import CalibrationRule, calibrate_forecast
from polytempo.model.distribution import build_distribution, probabilities_for_buckets
from polytempo.strategy.argmax_yes import ArgmaxYesStrategy
from polytempo.strategy.base import Strategy
from polytempo.strategy.decision import DecisionConfig
from polytempo.strategy.edge import MarketPrice, ProbabilityQuote, calculate_bucket_edges
from polytempo.weather.schema import ForecastValues


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


@dataclass(frozen=True)
class AnalysisResult:
    """Complete local analysis result."""

    distribution_mean_c: float
    distribution_sigma_c: float
    rows: list[AnalysisRow]


def analyze(
    input_data: AnalysisInput,
    strategy: Strategy | None = None,
) -> AnalysisResult:
    """Run the local analysis pipeline over already-provided inputs."""
    strategy = strategy if strategy is not None else ArgmaxYesStrategy()
    buckets = [parse_temperature_bucket(label) for label in input_data.bucket_labels]
    distribution = build_distribution(input_data.forecast_values_c)
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
            edge_yes_pp=edge.edge_yes_pp,
            action=decision.action,
            reason=decision.reason,
            confidence=decision.confidence,
            warnings=list(decision.warnings),
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
        rows=rows,
    )


def analyze_event(
    forecast: ForecastValues,
    event: PolymarketEvent,
    calibration_rule: CalibrationRule | None = None,
    decision_config: DecisionConfig | None = None,
    strategy: Strategy | None = None,
) -> AnalysisResult:
    """Run analysis over already-fetched weather and market inputs.

    Pass ``strategy`` to plug in an alternative rule set. ``decision_config``
    is a shortcut for tweaking the default ArgmaxYesStrategy thresholds and
    cannot be combined with ``strategy``.
    """
    if strategy is not None and decision_config is not None:
        raise ValueError("pass either strategy or decision_config, not both")
    if strategy is None:
        strategy = ArgmaxYesStrategy(config=decision_config or DecisionConfig())

    market_prices = to_market_prices(event)
    bucket_labels = [bucket.label for bucket in event.buckets]
    calibrated = calibrate_forecast(forecast, calibration_rule)
    buckets = [parse_temperature_bucket(label) for label in bucket_labels]
    distribution = build_distribution(calibrated.values_c)
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
            edge_yes_pp=edge.edge_yes_pp,
            action=decision.action,
            reason=decision.reason,
            confidence=decision.confidence,
            warnings=list(decision.warnings),
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
        rows=rows,
    )
