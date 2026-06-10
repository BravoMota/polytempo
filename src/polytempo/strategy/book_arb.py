"""Book-arbitrage strategy.

Model-free sum check over the whole event. Exactly one bucket resolves YES,
so if the asks across every bucket sum below $1 the full YES book locks in a
profit regardless of outcome; symmetrically, if the bids sum above $1 the
full NO book does. Buys equal shares of every bucket so the payout is the
same whichever bucket wins; trades nothing unless the whole book is quoted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from polytempo.strategy.decision import TradeDecision
from polytempo.strategy.edge import BucketEdge


@dataclass(frozen=True)
class BookArbConfig:
    """Thresholds for book_arb. ``min_profit_pp`` is the required gap between
    the book cost and $1, in pp of $1 — a noise buffer, not an edge gate.
    ``total_ticket_usd`` is split across the legs in proportion to entry price
    so every leg holds the same share count."""

    min_profit_pp: float = 2.0
    high_confidence_profit_pp: float = 5.0
    min_liquidity_usd: float = 25.0
    total_ticket_usd: float = 20.0


@dataclass(frozen=True)
class BookArbStrategy:
    """BUY the whole YES or NO book when its cost sum locks in profit; SKIP otherwise."""

    config: BookArbConfig = field(default_factory=BookArbConfig)
    name: str = "book_arb"

    def decide(self, edges: list[BucketEdge]) -> list[TradeDecision]:
        if not edges:
            return []
        cfg = self.config

        if len(edges) < 2:
            return _skip_all(edges, "incomplete book")

        profit_yes_pp = _yes_book_profit_pp(edges)
        profit_no_pp = _no_book_profit_pp(edges)
        if profit_yes_pp is None and profit_no_pp is None:
            return _skip_all(edges, "incomplete book")

        side, profit_pp = _better_book(profit_yes_pp, profit_no_pp)
        if profit_pp <= cfg.min_profit_pp:
            return _skip_all(edges, "book sum within noise")

        if any(
            e.liquidity_usd is None or e.liquidity_usd < cfg.min_liquidity_usd
            for e in edges
        ):
            return _skip_all(edges, "thin book")

        entry_prices = [_entry_price(e, side) for e in edges]
        total_cost = sum(entry_prices)
        if total_cost <= 0:
            return _skip_all(edges, "incomplete book")
        shares = cfg.total_ticket_usd / total_cost

        confidence = (
            "high" if profit_pp >= cfg.high_confidence_profit_pp else "medium"
        )
        action = "BUY_YES" if side == "YES" else "BUY_NO"
        return [
            TradeDecision(
                label=edge.label,
                action=action,
                reason="book sum arbitrage",
                edge_yes_pp=(
                    edge.edge_yes_pp if side == "YES" else edge.edge_no_pp
                ),
                confidence=confidence,
                side=side,
                stake_usd=shares * price,
            )
            for edge, price in zip(edges, entry_prices, strict=True)
        ]


def _yes_book_profit_pp(edges: list[BucketEdge]) -> float | None:
    """Profit in pp of $1 from buying YES on every bucket, or None if any ask
    is missing. Cost is the ask sum; the winning bucket pays $1 per share."""
    asks = [e.yes_ask for e in edges]
    if any(a is None for a in asks):
        return None
    return (1.0 - sum(asks)) * 100.0


def _no_book_profit_pp(edges: list[BucketEdge]) -> float | None:
    """Profit in pp of $1 from buying NO on every bucket, or None if any bid
    is missing. Cost is ``n - sum(bids)``; the ``n - 1`` losing buckets each
    pay $1 per share, so profit is ``sum(bids) - 1``."""
    bids = [e.yes_bid for e in edges]
    if any(b is None for b in bids):
        return None
    return (sum(bids) - 1.0) * 100.0


def _better_book(
    profit_yes_pp: float | None,
    profit_no_pp: float | None,
) -> tuple[str, float]:
    """Pick the more profitable computable book. Caller guarantees at least
    one side is not None."""
    if profit_no_pp is None:
        return "YES", profit_yes_pp
    if profit_yes_pp is None:
        return "NO", profit_no_pp
    if profit_yes_pp >= profit_no_pp:
        return "YES", profit_yes_pp
    return "NO", profit_no_pp


def _entry_price(edge: BucketEdge, side: str) -> float:
    """Entry price for one leg; quotes are present once the book is complete."""
    if side == "YES":
        return edge.yes_ask
    return 1.0 - edge.yes_bid


def _skip_all(edges: list[BucketEdge], reason: str) -> list[TradeDecision]:
    return [
        TradeDecision(
            label=edge.label,
            action="SKIP",
            reason=reason,
            edge_yes_pp=edge.edge_yes_pp,
            confidence="none",
            side="YES",
        )
        for edge in edges
    ]
