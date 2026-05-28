"""Argmax-YES strategy.

Bet only on the single bucket the model considers most likely. If that
bucket clears the edge + liquidity gate, BUY_YES it; every other bucket
is SKIPped. Avoids spraying stake across long-tail buckets just because
their edge is faintly positive.
"""

from __future__ import annotations

from dataclasses import dataclass

from polytempo.strategy.decision import DecisionConfig, TradeDecision, decide_bucket
from polytempo.strategy.edge import BucketEdge


@dataclass(frozen=True)
class ArgmaxYesStrategy:
    """BUY_YES on the model's argmax bucket if it passes the gate; SKIP the rest."""

    config: DecisionConfig = DecisionConfig()
    name: str = "argmax_yes"

    def decide(self, edges: list[BucketEdge]) -> list[TradeDecision]:
        if not edges:
            return []
        argmax_idx = max(
            range(len(edges)),
            key=lambda i: edges[i].model_probability,
        )
        decisions: list[TradeDecision] = []
        for i, edge in enumerate(edges):
            if i == argmax_idx:
                decisions.append(decide_bucket(edge, self.config))
            else:
                decisions.append(
                    TradeDecision(
                        label=edge.label,
                        action="SKIP",
                        reason="not argmax bucket",
                        edge_yes_pp=edge.edge_yes_pp,
                        confidence="none",
                    )
                )
        return decisions
