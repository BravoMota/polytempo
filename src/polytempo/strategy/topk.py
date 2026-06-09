"""Top-K edge strategy.

Generalizes argmax: instead of betting only the single best bucket, bet the
``k`` buckets carrying the largest edge on the chosen ``side`` that also clear
the edge + liquidity gate. Caps the number of simultaneous bets at ``k`` so
stake concentrates on the best mispricings without spraying the whole board.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from polytempo.strategy.decision import (
    DecisionConfig,
    TradeDecision,
    decide_bucket,
    decide_bucket_no,
)
from polytempo.strategy.edge import BucketEdge


@dataclass(frozen=True)
class TopKStrategy:
    """BUY the k highest-edge buckets on ``side`` that pass the gate; SKIP the rest."""

    k: int = 3
    side: str = "YES"
    config: DecisionConfig = DecisionConfig()
    name: str = "topk_yes"

    def decide(self, edges: list[BucketEdge]) -> list[TradeDecision]:
        if not edges:
            return []
        edge_pp = _edge_pp_for_side(self.side)
        gate = decide_bucket if self.side == "YES" else decide_bucket_no
        ranked = sorted(
            (i for i, e in enumerate(edges) if edge_pp(e) is not None),
            key=lambda i: edge_pp(edges[i]),
            reverse=True,
        )
        chosen = set(ranked[: self.k])
        decisions: list[TradeDecision] = []
        for i, edge in enumerate(edges):
            if i in chosen:
                decisions.append(gate(edge, self.config))
            else:
                decisions.append(
                    TradeDecision(
                        label=edge.label,
                        action="SKIP",
                        reason="not in top-k",
                        edge_yes_pp=edge_pp(edge),
                        confidence="none",
                        side=self.side,
                    )
                )
        return decisions


def _edge_pp_for_side(side: str) -> Callable[[BucketEdge], float | None]:
    if side == "YES":
        return lambda e: e.edge_yes_pp
    if side == "NO":
        return lambda e: e.edge_no_pp
    raise ValueError(f"side must be 'YES' or 'NO', got {side!r}")
