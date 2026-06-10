"""Tight-quote distribution-arbitrage strategy.

Same per-bucket better-of-YES/NO selection as dist_arb, but only trades
quotes it can trust: the spread must be present and tight, and the liquidity
floor is higher. The thesis is that dist_arb's long-lead "edge" often comes
from stale or wide quotes rather than real mispricing; this variant tests
that by refusing exactly those fills.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from polytempo.strategy.decision import TradeDecision
from polytempo.strategy.edge import BucketEdge


@dataclass(frozen=True)
class DistArbTightConfig:
    """Thresholds for dist_arb_tight. ``max_spread`` is a hard gate, not a
    warning: a missing or wide spread rejects the bucket."""

    min_edge_pp: float = 0.0
    high_confidence_edge_pp: float = 15.0
    min_liquidity_usd: float = 100.0
    max_spread: float = 0.05


@dataclass(frozen=True)
class DistArbTightStrategy:
    """Pick the better of YES/NO per bucket, only on tight quotes; SKIP otherwise."""

    config: DistArbTightConfig = field(default_factory=DistArbTightConfig)
    name: str = "dist_arb_tight"

    def decide(self, edges: list[BucketEdge]) -> list[TradeDecision]:
        return [_decide_bucket(e, self.config) for e in edges]


def _decide_bucket(edge: BucketEdge, cfg: DistArbTightConfig) -> TradeDecision:
    best_side, best_pp = _best_side(edge)

    if best_side is None:
        return _skip(edge, "missing edge", None, "YES")

    if best_pp <= cfg.min_edge_pp:
        return _skip(edge, "edge below threshold", best_pp, best_side)

    if edge.spread is None:
        return _skip(edge, "missing spread", best_pp, best_side)

    if edge.spread > cfg.max_spread:
        return _skip(edge, "spread above threshold", best_pp, best_side)

    if edge.liquidity_usd is None:
        return _skip(edge, "missing liquidity", best_pp, best_side)

    if edge.liquidity_usd < cfg.min_liquidity_usd:
        return _skip(edge, "liquidity below threshold", best_pp, best_side)

    confidence = "high" if best_pp >= cfg.high_confidence_edge_pp else "medium"
    action = "BUY_YES" if best_side == "YES" else "BUY_NO"
    return TradeDecision(
        label=edge.label,
        action=action,
        reason="edge, spread, and liquidity rules passed",
        edge_yes_pp=best_pp,
        confidence=confidence,
        side=best_side,
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


def _skip(
    edge: BucketEdge,
    reason: str,
    edge_pp: float | None,
    side: str,
) -> TradeDecision:
    return TradeDecision(
        label=edge.label,
        action="SKIP",
        reason=reason,
        edge_yes_pp=edge_pp,
        confidence="none",
        side=side,
    )
