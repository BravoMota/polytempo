"""Argmax-YES strategy.

Baseline strategy: buy YES on any bucket whose model edge clears the
configured threshold and liquidity floor. Mirrors the original
deterministic decision rules.
"""

from __future__ import annotations

from dataclasses import dataclass

from polytempo.strategy.decision import DecisionConfig, TradeDecision, decide_all
from polytempo.strategy.edge import BucketEdge


@dataclass(frozen=True)
class ArgmaxYesStrategy:
    """BUY_YES wherever edge and liquidity pass; SKIP otherwise."""

    config: DecisionConfig = DecisionConfig()
    name: str = "argmax_yes"

    def decide(self, edges: list[BucketEdge]) -> list[TradeDecision]:
        return decide_all(edges, self.config)
