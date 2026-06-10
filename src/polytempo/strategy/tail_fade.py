"""Tail-fade strategy.

Buys NO on the open-ended tail buckets ("N°C or below" / "N°C or higher")
when the market still bids a few cents for an outcome the model considers a
longshot — harvesting favorite-longshot bias. Selection is structural (tail
position), not edge-ranked, so it isolates whether NO trades win because of
the model or because longshots are systematically overpriced.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from polytempo.markets.buckets import parse_temperature_bucket
from polytempo.strategy.decision import TradeDecision
from polytempo.strategy.edge import BucketEdge

_TAIL_KINDS = frozenset({"or_below", "or_higher", "above"})


@dataclass(frozen=True)
class TailFadeConfig:
    """Thresholds for tail_fade. ``max_model_probability`` is the longshot
    ceiling for the tail; ``min_yes_bid`` keeps the NO entry from paying
    nearly $1 for nearly nothing."""

    max_model_probability: float = 0.05
    min_yes_bid: float = 0.03
    high_confidence_edge_pp: float = 15.0
    min_liquidity_usd: float = 25.0
    high_spread_warning: float = 0.10


@dataclass(frozen=True)
class TailFadeStrategy:
    """BUY_NO on overpriced open-ended tail buckets; SKIP everything else."""

    config: TailFadeConfig = field(default_factory=TailFadeConfig)
    name: str = "tail_fade"

    def decide(self, edges: list[BucketEdge]) -> list[TradeDecision]:
        return [_decide_bucket(e, self.config) for e in edges]


def _decide_bucket(edge: BucketEdge, cfg: TailFadeConfig) -> TradeDecision:
    warnings = _spread_warnings(edge, cfg)

    if not _is_tail(edge.label):
        return _skip(edge, "not a tail bucket", warnings)

    if edge.model_probability > cfg.max_model_probability:
        return _skip(edge, "model probability above threshold", warnings)

    if edge.yes_bid is None:
        return _skip(edge, "missing yes_bid", warnings)

    if edge.yes_bid < cfg.min_yes_bid:
        return _skip(edge, "yes_bid below threshold", warnings)

    if edge.edge_no_pp is None:
        return _skip(edge, "missing edge", warnings)

    if edge.edge_no_pp <= 0:
        return _skip(edge, "tail not overpriced", warnings)

    if edge.liquidity_usd is None:
        return _skip(edge, "missing liquidity", warnings)

    if edge.liquidity_usd < cfg.min_liquidity_usd:
        return _skip(edge, "liquidity below threshold", warnings)

    confidence = (
        "high" if edge.edge_no_pp >= cfg.high_confidence_edge_pp else "medium"
    )
    return TradeDecision(
        label=edge.label,
        action="BUY_NO",
        reason="tail overpriced against model",
        edge_yes_pp=edge.edge_no_pp,
        confidence=confidence,
        side="NO",
        warnings=warnings,
    )


def _is_tail(label: str) -> bool:
    """True for open-ended bucket labels; unparseable labels are not tails."""
    try:
        bucket = parse_temperature_bucket(label)
    except ValueError:
        return False
    return bucket.kind in _TAIL_KINDS


def _spread_warnings(edge: BucketEdge, cfg: TailFadeConfig) -> list[str]:
    if edge.spread is None:
        return []
    if edge.spread > cfg.high_spread_warning:
        return ["high spread"]
    return []


def _skip(edge: BucketEdge, reason: str, warnings: list[str]) -> TradeDecision:
    return TradeDecision(
        label=edge.label,
        action="SKIP",
        reason=reason,
        edge_yes_pp=edge.edge_no_pp,
        confidence="none",
        side="NO",
        warnings=warnings,
    )
