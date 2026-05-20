"""Strategy protocol.

A strategy turns per-bucket edge metrics into per-bucket trade decisions.
Concrete strategies live in their own modules and are plugged into the
analysis pipeline via ``analyze``/``analyze_event``.
"""

from __future__ import annotations

from typing import Protocol

from polytempo.strategy.decision import TradeDecision
from polytempo.strategy.edge import BucketEdge


class Strategy(Protocol):
    """Maps a list of bucket edges to a list of trade decisions, preserving order."""

    name: str

    def decide(self, edges: list[BucketEdge]) -> list[TradeDecision]: ...
