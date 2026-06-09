"""Sanity edge-band strategy.

Trades the better of YES/NO per bucket (like dist_arb) but only when the edge
sits inside a believable band. Edges at or below ``min_edge_pp`` are too thin
to bother; edges above ``max_edge_pp`` are treated as a red flag (stale quote
or model error) and SKIPped rather than chased.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from polytempo.strategy.decision import TradeDecision
from polytempo.strategy.edge import BucketEdge


@dataclass(frozen=True)
class EdgeBandConfig:
    """Thresholds for edge_band. ``max_edge_pp`` is the implausibility ceiling:
    an edge above it is rejected, not traded."""

    min_edge_pp: float = 5.0
    max_edge_pp: float = 25.0
    high_confidence_edge_pp: float = 15.0
    min_liquidity_usd: float = 25.0
    high_spread_warning: float = 0.10


@dataclass(frozen=True)
class EdgeBandStrategy:
    """BUY the richer side only when its edge is within the band; SKIP otherwise."""

    config: EdgeBandConfig = field(default_factory=EdgeBandConfig)
    name: str = "edge_band"

    def decide(self, edges: list[BucketEdge]) -> list[TradeDecision]:
        return [_decide_bucket(e, self.config) for e in edges]


def _decide_bucket(edge: BucketEdge, cfg: EdgeBandConfig) -> TradeDecision:
    warnings = _spread_warnings(edge, cfg)
    side, pp = _best_side(edge)

    if side is None:
        return _skip(edge, "missing edge", None, "YES", warnings)

    if pp <= cfg.min_edge_pp:
        return _skip(edge, "edge below band", pp, side, warnings)

    if pp > cfg.max_edge_pp:
        return _skip(edge, "edge above band", pp, side, warnings)

    if edge.liquidity_usd is None:
        return _skip(edge, "missing liquidity", pp, side, warnings)

    if edge.liquidity_usd < cfg.min_liquidity_usd:
        return _skip(edge, "liquidity below threshold", pp, side, warnings)

    confidence = "high" if pp >= cfg.high_confidence_edge_pp else "medium"
    action = "BUY_YES" if side == "YES" else "BUY_NO"
    return TradeDecision(
        label=edge.label,
        action=action,
        reason="edge within band and liquidity rules passed",
        edge_yes_pp=pp,
        confidence=confidence,
        side=side,
        warnings=warnings,
    )


def _best_side(edge: BucketEdge) -> tuple[str | None, float | None]:
    """Return the side with higher edge in pp, or (None, None) if neither
    side has a computable edge."""
    yes_pp = edge.edge_yes_pp
    no_pp = edge.edge_no_pp
    if yes_pp is None and no_pp is None:
        return None, None
    if no_pp is None:
        return "YES", yes_pp
    if yes_pp is None:
        return "NO", no_pp
    if yes_pp >= no_pp:
        return "YES", yes_pp
    return "NO", no_pp


def _spread_warnings(edge: BucketEdge, cfg: EdgeBandConfig) -> list[str]:
    if edge.spread is None:
        return []
    if edge.spread > cfg.high_spread_warning:
        return ["high spread"]
    return []


def _skip(
    edge: BucketEdge,
    reason: str,
    edge_pp: float | None,
    side: str,
    warnings: list[str],
) -> TradeDecision:
    return TradeDecision(
        label=edge.label,
        action="SKIP",
        reason=reason,
        edge_yes_pp=edge_pp,
        confidence="none",
        side=side,
        warnings=warnings,
    )
