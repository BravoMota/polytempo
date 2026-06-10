"""Coverage-band strategy.

Buys the model's credible interval as one basket: the smallest contiguous run
of buckets around the model peak holding at least ``target_mass`` probability.
The basket trades only when its combined model mass beats its combined ask
cost, so it stays profitable even when the model has the right distribution
but the wrong peak bucket — the failure mode that hurts argmax_yes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from polytempo.strategy.decision import TradeDecision
from polytempo.strategy.edge import BucketEdge


@dataclass(frozen=True)
class CoverageBandConfig:
    """Thresholds for coverage_band. ``min_basket_edge_pp`` gates the basket
    as a whole (``sum(p) - sum(ask)`` over the window, in pp); individual leg
    edges may be negative. ``flat_ticket_usd`` is the stake per leg."""

    target_mass: float = 0.80
    min_basket_edge_pp: float = 5.0
    high_confidence_edge_pp: float = 15.0
    min_liquidity_usd: float = 25.0
    flat_ticket_usd: float = 5.0


@dataclass(frozen=True)
class CoverageBandStrategy:
    """BUY_YES the credible-interval window when the basket edge clears; SKIP otherwise."""

    config: CoverageBandConfig = field(default_factory=CoverageBandConfig)
    name: str = "coverage_band"

    def decide(self, edges: list[BucketEdge]) -> list[TradeDecision]:
        if not edges:
            return []
        cfg = self.config
        lo, hi = _coverage_window(edges, cfg.target_mass)
        window = range(lo, hi + 1)

        reason = _basket_failure(edges, window, cfg)
        if reason is not None:
            return [
                _skip(e, reason if i in window else "not in coverage window")
                for i, e in enumerate(edges)
            ]

        basket_edge_pp = _basket_edge_pp(edges, window)
        confidence = (
            "high" if basket_edge_pp >= cfg.high_confidence_edge_pp else "medium"
        )
        decisions: list[TradeDecision] = []
        for i, edge in enumerate(edges):
            if i in window:
                decisions.append(
                    TradeDecision(
                        label=edge.label,
                        action="BUY_YES",
                        reason="basket edge and liquidity rules passed",
                        edge_yes_pp=edge.edge_yes_pp,
                        confidence=confidence,
                        side="YES",
                        stake_usd=cfg.flat_ticket_usd,
                    )
                )
            else:
                decisions.append(_skip(edge, "not in coverage window"))
        return decisions


def _coverage_window(edges: list[BucketEdge], target_mass: float) -> tuple[int, int]:
    """Smallest contiguous index window around the model peak with cumulative
    probability >= ``target_mass``. Grows toward the heavier neighbor; covers
    the whole event if the mass never reaches the target."""
    peak = max(range(len(edges)), key=lambda i: edges[i].model_probability)
    lo = hi = peak
    mass = edges[peak].model_probability
    while mass < target_mass and (lo > 0 or hi < len(edges) - 1):
        left_p = edges[lo - 1].model_probability if lo > 0 else None
        right_p = edges[hi + 1].model_probability if hi < len(edges) - 1 else None
        if right_p is None or (left_p is not None and left_p >= right_p):
            lo -= 1
            mass += left_p
        else:
            hi += 1
            mass += right_p
    return lo, hi


def _basket_failure(
    edges: list[BucketEdge],
    window: range,
    cfg: CoverageBandConfig,
) -> str | None:
    """Reason the whole basket is rejected, or None if it trades."""
    if any(edges[i].yes_ask is None for i in window):
        return "missing yes_ask in window"
    if _basket_edge_pp(edges, window) <= cfg.min_basket_edge_pp:
        return "basket edge below threshold"
    if any(edges[i].liquidity_usd is None for i in window):
        return "missing liquidity"
    if any(edges[i].liquidity_usd < cfg.min_liquidity_usd for i in window):
        return "liquidity below threshold"
    return None


def _basket_edge_pp(edges: list[BucketEdge], window: range) -> float:
    mass = sum(edges[i].model_probability for i in window)
    cost = sum(edges[i].yes_ask for i in window)
    return (mass - cost) * 100.0


def _skip(edge: BucketEdge, reason: str) -> TradeDecision:
    return TradeDecision(
        label=edge.label,
        action="SKIP",
        reason=reason,
        edge_yes_pp=edge.edge_yes_pp,
        confidence="none",
        side="YES",
    )
