"""Best-single-bet strategy.

Scans every bucket on both sides and trades only the single richest edge in
the whole event; SKIPs everything else. Maximum concentration -- one ticket
per event on the market's biggest mispricing, whichever side it falls on.
"""

from __future__ import annotations

from dataclasses import dataclass

from polytempo.strategy.decision import (
    DecisionConfig,
    TradeDecision,
    decide_bucket,
    decide_bucket_no,
)
from polytempo.strategy.edge import BucketEdge


@dataclass(frozen=True)
class MaxEdgeStrategy:
    """BUY the single highest-edge bucket/side that passes the gate; SKIP the rest."""

    config: DecisionConfig = DecisionConfig()
    name: str = "max_edge"

    def decide(self, edges: list[BucketEdge]) -> list[TradeDecision]:
        if not edges:
            return []
        best_i: int | None = None
        best_pp: float | None = None
        for i, edge in enumerate(edges):
            _, pp = _best_side(edge)
            if pp is None:
                continue
            if best_pp is None or pp > best_pp:
                best_i, best_pp = i, pp
        decisions: list[TradeDecision] = []
        for i, edge in enumerate(edges):
            side, pp = _best_side(edge)
            if i == best_i:
                gate = decide_bucket if side == "YES" else decide_bucket_no
                decisions.append(gate(edge, self.config))
            else:
                decisions.append(
                    TradeDecision(
                        label=edge.label,
                        action="SKIP",
                        reason="not max_edge bucket",
                        edge_yes_pp=pp,
                        confidence="none",
                        side=side or "YES",
                    )
                )
        return decisions


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
