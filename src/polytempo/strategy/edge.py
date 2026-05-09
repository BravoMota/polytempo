"""Edge calculation.

Compares model bucket probabilities against executable market prices.
Does not fetch markets, compute forecasts, or make trading decisions.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProbabilityQuote:
    """Model-implied probability for a bucket."""

    label: str
    probability: float


@dataclass(frozen=True)
class MarketPrice:
    """Executable YES prices and optional liquidity/spread metadata."""

    label: str
    yes_bid: float | None
    yes_ask: float | None
    liquidity_usd: float | None = None
    spread: float | None = None


@dataclass(frozen=True)
class BucketEdge:
    """Per-bucket model vs market comparison."""

    label: str
    model_probability: float
    yes_bid: float | None
    yes_ask: float | None
    edge_yes: float | None
    edge_yes_pp: float | None
    liquidity_usd: float | None
    spread: float | None


def calculate_yes_edge(model_probability: float, yes_ask: float) -> float:
    """Edge for buying YES: model probability minus executable YES ask."""
    _validate_unit_interval(model_probability, "model_probability")
    _validate_unit_interval(yes_ask, "yes_ask")
    return model_probability - yes_ask


def _validate_unit_interval(value: float, name: str) -> None:
    if not (0.0 <= value <= 1.0):
        raise ValueError(f"{name} must be between 0 and 1 inclusive, got {value!r}")


def calculate_bucket_edges(
    probabilities: list[ProbabilityQuote],
    market_prices: list[MarketPrice],
) -> list[BucketEdge]:
    """Join model probabilities to market rows by label; order follows probabilities."""
    by_label: dict[str, MarketPrice] = {}
    for mp in market_prices:
        by_label.setdefault(mp.label, mp)

    out: list[BucketEdge] = []
    for pq in probabilities:
        mp = by_label.get(pq.label)
        if mp is None:
            out.append(
                BucketEdge(
                    label=pq.label,
                    model_probability=pq.probability,
                    yes_bid=None,
                    yes_ask=None,
                    edge_yes=None,
                    edge_yes_pp=None,
                    liquidity_usd=None,
                    spread=None,
                )
            )
            continue

        yes_ask = mp.yes_ask
        if yes_ask is None:
            edge_yes = None
            edge_yes_pp = None
        else:
            edge_yes = calculate_yes_edge(pq.probability, yes_ask)
            edge_yes_pp = edge_yes * 100.0

        out.append(
            BucketEdge(
                label=pq.label,
                model_probability=pq.probability,
                yes_bid=mp.yes_bid,
                yes_ask=yes_ask,
                edge_yes=edge_yes,
                edge_yes_pp=edge_yes_pp,
                liquidity_usd=mp.liquidity_usd,
                spread=mp.spread,
            )
        )

    return out
