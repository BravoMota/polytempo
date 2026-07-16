"""Distribution-directed side selection.

Model mass assigns each bucket a role (core / believe_tail / fade / ignore).
That role locks the trade side; the market only vetoes unprofitable prices.
Never buys NO on the mode/core, and never buys YES on junk tails just because
the ask is cheap — opposite of edge-first strategies like dist_arb.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from polytempo.strategy.decision import TradeDecision
from polytempo.strategy.edge import BucketEdge


class _Role(str, Enum):
    CORE = "core"
    BELIEVE_TAIL = "believe_tail"
    FADE = "fade"
    IGNORE = "ignore"


@dataclass(frozen=True)
class MassSideConfig:
    """Thresholds for mass_side.

    Roles use only ``model_probability``. Price caps and ``min_edge_pp`` are
    profitability gates that never flip YES↔NO. Believed-tail YES tickets use
    a flat ``believe_tail_stake_usd`` floor so the view is expressed cheaply.
    """

    core_alpha: float = 0.5
    p_fade: float = 0.05
    p_believe: float = 0.08
    max_yes_ask: float = 0.90
    max_no_price: float = 0.85
    min_edge_pp: float = 0.0
    high_confidence_edge_pp: float = 15.0
    min_liquidity_usd: float = 25.0
    high_spread_warning: float = 0.10
    believe_tail_stake_usd: float = 1.0


@dataclass(frozen=True)
class MassSideStrategy:
    """Side from model mass; market gates price/EV; SKIP otherwise."""

    config: MassSideConfig = field(default_factory=MassSideConfig)
    name: str = "mass_side"

    def decide(self, edges: list[BucketEdge]) -> list[TradeDecision]:
        roles = _assign_roles(edges, self.config)
        return [
            _decide_bucket(edge, roles[i], self.config)
            for i, edge in enumerate(edges)
        ]


def _assign_roles(edges: list[BucketEdge], cfg: MassSideConfig) -> list[_Role]:
    """Classify each bucket from model mass alone (order preserved)."""
    if not edges:
        return []
    if cfg.core_alpha <= 0 or cfg.core_alpha > 1:
        raise ValueError("core_alpha must be in (0, 1]")
    if cfg.p_fade < 0 or cfg.p_believe < 0:
        raise ValueError("p_fade and p_believe must be non-negative")

    p_mode = max(e.model_probability for e in edges)
    core_floor = cfg.core_alpha * p_mode
    roles: list[_Role] = []
    for edge in edges:
        p = edge.model_probability
        if p >= core_floor and p_mode > 0:
            roles.append(_Role.CORE)
        elif p >= cfg.p_believe:
            roles.append(_Role.BELIEVE_TAIL)
        elif p <= cfg.p_fade:
            roles.append(_Role.FADE)
        else:
            roles.append(_Role.IGNORE)
    return roles


def _decide_bucket(
    edge: BucketEdge,
    role: _Role,
    cfg: MassSideConfig,
) -> TradeDecision:
    warnings = _spread_warnings(edge, cfg)

    if role == _Role.IGNORE:
        return _skip(edge, "mass role ignore", None, "YES", warnings)

    if role in (_Role.CORE, _Role.BELIEVE_TAIL):
        return _decide_yes(edge, role, cfg, warnings)
    return _decide_no(edge, cfg, warnings)


def _decide_yes(
    edge: BucketEdge,
    role: _Role,
    cfg: MassSideConfig,
    warnings: list[str],
) -> TradeDecision:
    if edge.yes_ask is None:
        return _skip(edge, "missing yes_ask", edge.edge_yes_pp, "YES", warnings)
    if edge.yes_ask > cfg.max_yes_ask:
        return _skip(edge, "yes_ask above max", edge.edge_yes_pp, "YES", warnings)
    if edge.edge_yes_pp is None:
        return _skip(edge, "missing edge", None, "YES", warnings)
    if edge.edge_yes_pp <= cfg.min_edge_pp:
        return _skip(edge, "edge below threshold", edge.edge_yes_pp, "YES", warnings)
    if edge.liquidity_usd is None:
        return _skip(edge, "missing liquidity", edge.edge_yes_pp, "YES", warnings)
    if edge.liquidity_usd < cfg.min_liquidity_usd:
        return _skip(
            edge, "liquidity below threshold", edge.edge_yes_pp, "YES", warnings
        )

    confidence = (
        "high" if edge.edge_yes_pp >= cfg.high_confidence_edge_pp else "medium"
    )
    reason = (
        "core mass yes"
        if role == _Role.CORE
        else "believed-tail yes"
    )
    stake_usd = (
        cfg.believe_tail_stake_usd if role == _Role.BELIEVE_TAIL else None
    )
    return TradeDecision(
        label=edge.label,
        action="BUY_YES",
        reason=reason,
        edge_yes_pp=edge.edge_yes_pp,
        confidence=confidence,
        side="YES",
        stake_usd=stake_usd,
        warnings=warnings,
    )


def _decide_no(
    edge: BucketEdge,
    cfg: MassSideConfig,
    warnings: list[str],
) -> TradeDecision:
    if edge.yes_bid is None:
        return _skip(edge, "missing yes_bid", edge.edge_no_pp, "NO", warnings)
    no_price = 1.0 - edge.yes_bid
    if no_price > cfg.max_no_price:
        return _skip(edge, "no price above max", edge.edge_no_pp, "NO", warnings)
    if edge.edge_no_pp is None:
        return _skip(edge, "missing edge", None, "NO", warnings)
    if edge.edge_no_pp <= cfg.min_edge_pp:
        return _skip(edge, "edge below threshold", edge.edge_no_pp, "NO", warnings)
    if edge.liquidity_usd is None:
        return _skip(edge, "missing liquidity", edge.edge_no_pp, "NO", warnings)
    if edge.liquidity_usd < cfg.min_liquidity_usd:
        return _skip(
            edge, "liquidity below threshold", edge.edge_no_pp, "NO", warnings
        )

    confidence = (
        "high" if edge.edge_no_pp >= cfg.high_confidence_edge_pp else "medium"
    )
    return TradeDecision(
        label=edge.label,
        action="BUY_NO",
        reason="fade low-mass no",
        edge_yes_pp=edge.edge_no_pp,
        confidence=confidence,
        side="NO",
        warnings=warnings,
    )


def _spread_warnings(edge: BucketEdge, cfg: MassSideConfig) -> list[str]:
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
