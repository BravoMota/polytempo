"""Best-return strategy.

Like max_edge scans every bucket on both sides, but ranks by expected return
on stake instead of edge in pp: a 5pp edge on a 5-cent bucket doubles the
stake in expectation while the same edge at 50 cents returns 10%. A win-
probability floor keeps the ranking from chasing pure longshots whose ROI
explodes as the entry price approaches zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from polytempo.strategy.decision import TradeDecision
from polytempo.strategy.edge import BucketEdge


@dataclass(frozen=True)
class MaxRoiConfig:
    """Thresholds for max_roi. ROI is the expected return per dollar staked
    (``win_probability / entry_price - 1``). ``min_win_probability`` floors
    the chosen side's model win probability."""

    min_roi: float = 0.05
    high_confidence_roi: float = 0.25
    min_win_probability: float = 0.05
    min_liquidity_usd: float = 25.0


@dataclass(frozen=True)
class MaxRoiStrategy:
    """BUY the single highest-ROI bucket/side that passes the gate; SKIP the rest."""

    config: MaxRoiConfig = field(default_factory=MaxRoiConfig)
    name: str = "max_roi"

    def decide(self, edges: list[BucketEdge]) -> list[TradeDecision]:
        if not edges:
            return []
        cfg = self.config
        best_i: int | None = None
        best_side: str | None = None
        best_roi: float | None = None
        for i, edge in enumerate(edges):
            side, roi = _best_roi_side(edge, cfg.min_win_probability)
            if roi is None:
                continue
            if best_roi is None or roi > best_roi:
                best_i, best_side, best_roi = i, side, roi

        decisions: list[TradeDecision] = []
        for i, edge in enumerate(edges):
            if i == best_i:
                decisions.append(_decide_best(edge, best_side, best_roi, cfg))
            else:
                side, _ = _best_roi_side(edge, cfg.min_win_probability)
                decisions.append(
                    _skip(edge, "not max_roi bucket", side or "YES")
                )
        return decisions


def _decide_best(
    edge: BucketEdge,
    side: str,
    roi: float,
    cfg: MaxRoiConfig,
) -> TradeDecision:
    edge_pp = edge.edge_yes_pp if side == "YES" else edge.edge_no_pp

    if roi <= cfg.min_roi:
        return _skip(edge, "roi below threshold", side, edge_pp)

    if edge.liquidity_usd is None:
        return _skip(edge, "missing liquidity", side, edge_pp)

    if edge.liquidity_usd < cfg.min_liquidity_usd:
        return _skip(edge, "liquidity below threshold", side, edge_pp)

    confidence = "high" if roi >= cfg.high_confidence_roi else "medium"
    action = "BUY_YES" if side == "YES" else "BUY_NO"
    return TradeDecision(
        label=edge.label,
        action=action,
        reason="roi and liquidity rules passed",
        edge_yes_pp=edge_pp,
        confidence=confidence,
        side=side,
    )


def _best_roi_side(
    edge: BucketEdge,
    min_win_probability: float,
) -> tuple[str | None, float | None]:
    """Return the side with higher expected ROI, or (None, None) if neither
    side has a priced quote with enough model win probability."""
    p = edge.model_probability
    roi_yes: float | None = None
    if edge.yes_ask is not None and edge.yes_ask > 0 and p >= min_win_probability:
        roi_yes = p / edge.yes_ask - 1.0
    roi_no: float | None = None
    if (
        edge.yes_bid is not None
        and edge.yes_bid < 1.0
        and (1.0 - p) >= min_win_probability
    ):
        roi_no = (1.0 - p) / (1.0 - edge.yes_bid) - 1.0
    if roi_yes is None and roi_no is None:
        return None, None
    if roi_no is None:
        return "YES", roi_yes
    if roi_yes is None:
        return "NO", roi_no
    if roi_yes >= roi_no:
        return "YES", roi_yes
    return "NO", roi_no


def _skip(
    edge: BucketEdge,
    reason: str,
    side: str,
    edge_pp: float | None = None,
) -> TradeDecision:
    if edge_pp is None:
        edge_pp = edge.edge_yes_pp if side == "YES" else edge.edge_no_pp
    return TradeDecision(
        label=edge.label,
        action="SKIP",
        reason=reason,
        edge_yes_pp=edge_pp,
        confidence="none",
        side=side,
    )
