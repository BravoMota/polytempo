"""Argmax-NO strategy.

NO-side mirror of ``argmax_yes``. Bet NO only on the single bucket carrying
the largest NO edge (``edge_no_pp = yes_bid - p_model`` — i.e. the bucket the
market most overprices relative to the model). If that bucket clears the
edge + liquidity gate, BUY_NO it; every other bucket is SKIPped. Concentrates
the NO bet on the best mispricing instead of spraying across the tails.
"""

from __future__ import annotations

from dataclasses import dataclass

from polytempo.strategy.decision import DecisionConfig, TradeDecision, decide_bucket_no
from polytempo.strategy.edge import BucketEdge


@dataclass(frozen=True)
class ArgmaxNoStrategy:
    """BUY_NO on the highest-NO-edge bucket if it passes the gate; SKIP the rest."""

    config: DecisionConfig = DecisionConfig()
    name: str = "argmax_no"

    def decide(self, edges: list[BucketEdge]) -> list[TradeDecision]:
        if not edges:
            return []
        candidates = [i for i, e in enumerate(edges) if e.edge_no_pp is not None]
        argmax_idx = (
            max(candidates, key=lambda i: edges[i].edge_no_pp)
            if candidates
            else None
        )
        decisions: list[TradeDecision] = []
        for i, edge in enumerate(edges):
            if i == argmax_idx:
                decisions.append(decide_bucket_no(edge, self.config))
            else:
                decisions.append(
                    TradeDecision(
                        label=edge.label,
                        action="SKIP",
                        reason="not argmax_no bucket",
                        edge_yes_pp=edge.edge_no_pp,
                        confidence="none",
                        side="NO",
                    )
                )
        return decisions
