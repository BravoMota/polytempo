"""Kelly-sized distribution-arbitrage strategy.

Identical bucket/side selection to dist_arb; only the sizing differs. Instead
of the ledger's 2-5% edge ramp, each trade stakes a fractional Kelly bet:
for a binary payout at entry price ``c`` and model win probability ``p`` the
full-Kelly bankroll fraction is ``(p - c) / (1 - c)``, scaled down by
``kelly_multiplier`` and capped at ``max_stake_fraction`` (the ramp ceiling,
so the two sizing schemes stay comparable).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from polytempo.strategy.decision import TradeDecision
from polytempo.strategy.edge import BucketEdge


@dataclass(frozen=True)
class DistArbKellyConfig:
    """Thresholds for dist_arb_kelly. Selection gates match DistArbConfig;
    ``kelly_multiplier`` and ``max_stake_fraction`` control sizing via
    ``TradeDecision.stake_fraction``, which the ledger must honor."""

    min_edge_pp: float = 0.0
    high_confidence_edge_pp: float = 15.0
    min_liquidity_usd: float = 25.0
    high_spread_warning: float = 0.10
    kelly_multiplier: float = 0.25
    max_stake_fraction: float = 0.05


@dataclass(frozen=True)
class DistArbKellyStrategy:
    """Pick the better of YES/NO per bucket, staking fractional Kelly; SKIP otherwise."""

    config: DistArbKellyConfig = field(default_factory=DistArbKellyConfig)
    name: str = "dist_arb_kelly"

    def decide(self, edges: list[BucketEdge]) -> list[TradeDecision]:
        return [_decide_bucket(e, self.config) for e in edges]


def _decide_bucket(edge: BucketEdge, cfg: DistArbKellyConfig) -> TradeDecision:
    warnings = _spread_warnings(edge, cfg)
    best_side, best_pp = _best_side(edge)

    if best_side is None:
        return _skip(edge, "missing edge", None, "YES", warnings)

    if best_pp <= cfg.min_edge_pp:
        return _skip(edge, "edge below threshold", best_pp, best_side, warnings)

    if edge.liquidity_usd is None:
        return _skip(edge, "missing liquidity", best_pp, best_side, warnings)

    if edge.liquidity_usd < cfg.min_liquidity_usd:
        return _skip(edge, "liquidity below threshold", best_pp, best_side, warnings)

    fraction = _kelly_fraction(edge, best_side, cfg)
    if fraction is None or fraction <= 0:
        return _skip(edge, "kelly fraction not positive", best_pp, best_side, warnings)

    confidence = "high" if best_pp >= cfg.high_confidence_edge_pp else "medium"
    action = "BUY_YES" if best_side == "YES" else "BUY_NO"
    return TradeDecision(
        label=edge.label,
        action=action,
        reason="edge and liquidity rules passed",
        edge_yes_pp=best_pp,
        confidence=confidence,
        side=best_side,
        stake_fraction=fraction,
        warnings=warnings,
    )


def _kelly_fraction(
    edge: BucketEdge,
    side: str,
    cfg: DistArbKellyConfig,
) -> float | None:
    """Capped fractional-Kelly bankroll fraction for the chosen side, or None
    if the entry price leaves no room for a payout."""
    if side == "YES":
        win_probability = edge.model_probability
        entry_price = edge.yes_ask
    else:
        win_probability = 1.0 - edge.model_probability
        entry_price = None if edge.yes_bid is None else 1.0 - edge.yes_bid
    if entry_price is None or entry_price >= 1.0:
        return None
    full_kelly = (win_probability - entry_price) / (1.0 - entry_price)
    return min(cfg.kelly_multiplier * full_kelly, cfg.max_stake_fraction)


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


def _spread_warnings(edge: BucketEdge, cfg: DistArbKellyConfig) -> list[str]:
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
