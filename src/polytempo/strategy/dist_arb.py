"""Distribution-arbitrage strategy.

For each bucket, compares model probability against both market sides and
buys whichever side carries more edge. YES leg pays at ``yes_ask``; NO leg
pays at ``1 - yes_bid``. SKIPs the bucket if neither side clears the edge
gate or liquidity is too thin to trust the quote.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from polytempo.strategy.decision import TradeDecision
from polytempo.strategy.edge import BucketEdge


@dataclass(frozen=True)
class DistArbConfig:
    """Thresholds for dist_arb. Liquidity floor is a stale-quote filter, not a
    sizing constraint — paper mode fills at the quoted price regardless."""

    min_edge_pp: float = 0.0
    high_confidence_edge_pp: float = 15.0
    min_liquidity_usd: float = 25.0
    high_spread_warning: float = 0.10


@dataclass(frozen=True)
class DistArbStrategy:
    """Pick the better of YES/NO for each bucket; SKIP otherwise."""

    config: DistArbConfig = field(default_factory=DistArbConfig)
    name: str = "dist_arb"

    def decide(self, edges: list[BucketEdge]) -> list[TradeDecision]:
        return [_decide_bucket(e, self.config) for e in edges]


def _decide_bucket(edge: BucketEdge, cfg: DistArbConfig) -> TradeDecision:
    warnings = _spread_warnings(edge, cfg)
    best_side, best_pp = _best_side(edge)

    if best_side is None:
        return _skip(edge, "missing edge", None, warnings)

    if best_pp <= cfg.min_edge_pp:
        return _skip(edge, "edge below threshold", best_pp, warnings)

    if edge.liquidity_usd is None:
        return _skip(edge, "missing liquidity", best_pp, warnings)

    if edge.liquidity_usd < cfg.min_liquidity_usd:
        return _skip(edge, "liquidity below threshold", best_pp, warnings)

    confidence = "high" if best_pp >= cfg.high_confidence_edge_pp else "medium"
    action = "BUY_YES" if best_side == "YES" else "BUY_NO"
    return TradeDecision(
        label=edge.label,
        action=action,
        reason="edge and liquidity rules passed",
        edge_yes_pp=best_pp,
        confidence=confidence,
        side=best_side,
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


def _spread_warnings(edge: BucketEdge, cfg: DistArbConfig) -> list[str]:
    if edge.spread is None:
        return []
    if edge.spread > cfg.high_spread_warning:
        return ["high spread"]
    return []


def _skip(
    edge: BucketEdge,
    reason: str,
    edge_pp: float | None,
    warnings: list[str],
) -> TradeDecision:
    return TradeDecision(
        label=edge.label,
        action="SKIP",
        reason=reason,
        edge_yes_pp=edge_pp,
        confidence="none",
        side="YES",
        warnings=warnings,
    )
