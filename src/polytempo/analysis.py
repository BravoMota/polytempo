"""Local analysis use case.

Connects bucket parsing, distribution math, edge calculation, and decision rules.
This module uses local inputs only; it does not fetch forecasts or markets.
"""

from __future__ import annotations

from dataclasses import dataclass

from polytempo.markets.buckets import parse_temperature_bucket
from polytempo.model.distribution import build_distribution, probabilities_for_buckets
from polytempo.strategy.decision import decide_all
from polytempo.strategy.edge import MarketPrice, ProbabilityQuote, calculate_bucket_edges


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


def analyze(input_data: AnalysisInput) -> AnalysisResult:
    """Run the local analysis pipeline over already-provided inputs."""
    buckets = [parse_temperature_bucket(label) for label in input_data.bucket_labels]
    distribution = build_distribution(input_data.forecast_values_c)
    probabilities = probabilities_for_buckets(distribution, buckets)
    quotes = [
        ProbabilityQuote(label=p.label, probability=p.probability)
        for p in probabilities
    ]
    edges = calculate_bucket_edges(quotes, input_data.market_prices)
    decisions = decide_all(edges)

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
