"""Mid-band strategy.

YES-only on buckets whose ``yes_ask`` sits in a configurable price band.
Buys with a flat ticket regardless of edge size — the thesis is "harvest
many small trades in the boring middle", so the edge ramp used by the other
strategies does not apply here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from polytempo.strategy.decision import TradeDecision
from polytempo.strategy.edge import BucketEdge


@dataclass(frozen=True)
class MidBandConfig:
    """Thresholds for mid_band.

    The band is inclusive on both ends. ``flat_ticket_usd`` overrides the
    ledger's edge ramp; the ledger must honor ``TradeDecision.stake_usd``.
    """

    price_min: float = 0.20
    price_max: float = 0.60
    min_edge_pp: float = 0.0
    high_confidence_edge_pp: float = 15.0
    min_liquidity_usd: float = 50.0
    flat_ticket_usd: float = 5.0
    high_spread_warning: float = 0.10


@dataclass(frozen=True)
class MidBandStrategy:
    """BUY_YES with a flat ticket inside the price band; SKIP otherwise."""

    config: MidBandConfig = field(default_factory=MidBandConfig)
    name: str = "mid_band"

    def decide(self, edges: list[BucketEdge]) -> list[TradeDecision]:
        return [_decide_bucket(e, self.config) for e in edges]


def _decide_bucket(edge: BucketEdge, cfg: MidBandConfig) -> TradeDecision:
    warnings = _spread_warnings(edge, cfg)

    if edge.yes_ask is None:
        return _skip(edge, "missing yes_ask", None, warnings)

    if edge.yes_ask < cfg.price_min or edge.yes_ask > cfg.price_max:
        return _skip(edge, "price outside band", edge.edge_yes_pp, warnings)

    if edge.edge_yes_pp is None:
        return _skip(edge, "missing edge", None, warnings)

    if edge.edge_yes_pp <= cfg.min_edge_pp:
        return _skip(edge, "edge below threshold", edge.edge_yes_pp, warnings)

    if edge.liquidity_usd is None:
        return _skip(edge, "missing liquidity", edge.edge_yes_pp, warnings)

    if edge.liquidity_usd < cfg.min_liquidity_usd:
        return _skip(edge, "liquidity below threshold", edge.edge_yes_pp, warnings)

    confidence = (
        "high" if edge.edge_yes_pp >= cfg.high_confidence_edge_pp else "medium"
    )
    return TradeDecision(
        label=edge.label,
        action="BUY_YES",
        reason="edge and liquidity rules passed",
        edge_yes_pp=edge.edge_yes_pp,
        confidence=confidence,
        side="YES",
        stake_usd=cfg.flat_ticket_usd,
        warnings=warnings,
    )


def _spread_warnings(edge: BucketEdge, cfg: MidBandConfig) -> list[str]:
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
        stake_usd=None,
        warnings=warnings,
    )
