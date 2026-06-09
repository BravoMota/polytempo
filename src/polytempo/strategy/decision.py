"""Deterministic decision rules.

Maps per-bucket edge results to BUY_YES or SKIP using fixed thresholds.
Spread is informational only and never blocks a BUY_YES.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from polytempo.strategy.edge import BucketEdge


@dataclass(frozen=True)
class DecisionConfig:
    """Thresholds for deterministic trade recommendations.

    ``min_edge_pp`` is the BUY gate. ``high_confidence_edge_pp`` only labels
    the row's confidence and has no effect on sizing (the ledger's edge ramp
    handles stake scaling independently).
    """

    min_edge_pp: float = 0.0
    high_confidence_edge_pp: float = 15.0
    min_liquidity_usd: float = 100.0
    high_spread_warning: float = 0.10


@dataclass(frozen=True)
class TradeDecision:
    """Recommendation for one bucket."""

    label: str
    action: str
    reason: str
    edge_yes_pp: float | None
    confidence: str
    side: str = "YES"
    stake_usd: float | None = None
    warnings: list[str] = field(default_factory=list)


def decide_bucket(edge: BucketEdge, config: DecisionConfig | None = None) -> TradeDecision:
    """Return BUY_YES or SKIP from edge metrics and thresholds."""
    cfg = config if config is not None else DecisionConfig()
    warnings = _spread_warnings(edge, cfg)

    if edge.edge_yes_pp is None:
        return TradeDecision(
            label=edge.label,
            action="SKIP",
            reason="missing edge",
            edge_yes_pp=None,
            confidence="none",
            warnings=warnings,
        )

    if edge.edge_yes_pp <= cfg.min_edge_pp:
        return TradeDecision(
            label=edge.label,
            action="SKIP",
            reason="edge below threshold",
            edge_yes_pp=edge.edge_yes_pp,
            confidence="none",
            warnings=warnings,
        )

    if edge.liquidity_usd is None:
        return TradeDecision(
            label=edge.label,
            action="SKIP",
            reason="missing liquidity",
            edge_yes_pp=edge.edge_yes_pp,
            confidence="none",
            warnings=warnings,
        )

    if edge.liquidity_usd < cfg.min_liquidity_usd:
        return TradeDecision(
            label=edge.label,
            action="SKIP",
            reason="liquidity below threshold",
            edge_yes_pp=edge.edge_yes_pp,
            confidence="none",
            warnings=warnings,
        )

    confidence = (
        "high" if edge.edge_yes_pp >= cfg.high_confidence_edge_pp else "medium"
    )
    return TradeDecision(
        label=edge.label,
        action="BUY_YES",
        reason="edge and liquidity rules passed",
        edge_yes_pp=edge.edge_yes_pp,
        confidence=confidence,
        warnings=warnings,
    )


def decide_bucket_no(edge: BucketEdge, config: DecisionConfig | None = None) -> TradeDecision:
    """Return BUY_NO or SKIP from NO-side edge metrics and thresholds.

    Mirror of :func:`decide_bucket` on the NO side. NO fills at ``1 - yes_bid``
    and its edge is ``yes_bid - p_model`` (``edge.edge_no_pp``). The chosen
    side's edge is reported in ``edge_yes_pp`` (same convention as dist_arb).
    """
    cfg = config if config is not None else DecisionConfig()
    warnings = _spread_warnings(edge, cfg)

    if edge.edge_no_pp is None:
        return TradeDecision(
            label=edge.label,
            action="SKIP",
            reason="missing edge",
            edge_yes_pp=None,
            confidence="none",
            side="NO",
            warnings=warnings,
        )

    if edge.edge_no_pp <= cfg.min_edge_pp:
        return TradeDecision(
            label=edge.label,
            action="SKIP",
            reason="edge below threshold",
            edge_yes_pp=edge.edge_no_pp,
            confidence="none",
            side="NO",
            warnings=warnings,
        )

    if edge.liquidity_usd is None:
        return TradeDecision(
            label=edge.label,
            action="SKIP",
            reason="missing liquidity",
            edge_yes_pp=edge.edge_no_pp,
            confidence="none",
            side="NO",
            warnings=warnings,
        )

    if edge.liquidity_usd < cfg.min_liquidity_usd:
        return TradeDecision(
            label=edge.label,
            action="SKIP",
            reason="liquidity below threshold",
            edge_yes_pp=edge.edge_no_pp,
            confidence="none",
            side="NO",
            warnings=warnings,
        )

    confidence = (
        "high" if edge.edge_no_pp >= cfg.high_confidence_edge_pp else "medium"
    )
    return TradeDecision(
        label=edge.label,
        action="BUY_NO",
        reason="edge and liquidity rules passed",
        edge_yes_pp=edge.edge_no_pp,
        confidence=confidence,
        side="NO",
        warnings=warnings,
    )


def _spread_warnings(edge: BucketEdge, cfg: DecisionConfig) -> list[str]:
    if edge.spread is None:
        return []
    if edge.spread > cfg.high_spread_warning:
        return ["high spread"]
    return []


def decide_all(
    edges: list[BucketEdge],
    config: DecisionConfig | None = None,
) -> list[TradeDecision]:
    """Apply decide_bucket to each edge in order."""
    cfg = config if config is not None else DecisionConfig()
    return [decide_bucket(e, cfg) for e in edges]
