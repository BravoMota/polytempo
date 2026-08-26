"""Depth-aware marketable-limit sizing for the live execution stack.

Given a full-depth book, decides how many shares to quote and at what limit
price so a single limit order crosses the resting asks up to a slippage/price
cap without walking deeper than we're willing to pay.
"""

from __future__ import annotations

import math

from polytempo.live.models import BookDepth, SizedOrder


def walk_cap(
    max_slippage: float,
    *,
    edge_pp: float | None = None,
    edge_fraction: float | None = None,
) -> float:
    """Ask-walk allowed when sizing, in price units.

    With ``edge_fraction`` unset this is ``max_slippage`` (prod A). With it set,
    walk ``min(max_slippage, fraction * edge)`` so a 20¢ edge at fraction 0.25
    may pay 5¢ above best ask, while a 2¢ edge barely walks. ``edge_pp`` is
    percentage points (16.8 → 0.168).
    """
    if edge_fraction is None:
        return max_slippage
    edge = 0.0 if edge_pp is None else max(0.0, float(edge_pp) / 100.0)
    return min(max_slippage, edge_fraction * edge)


def size_buy(
    book: BookDepth,
    stake_usd: float,
    max_slippage: float,
    min_depth_usd: float,
    max_price: float,
) -> SizedOrder | None:
    """Size a marketable-limit BUY of ``book``'s token against its resting asks.

    The price window is the asks priced at or below
    ``min(best_ask + max_slippage, max_price)``. Returns ``None`` when there are
    no asks, the window is empty, resting depth in the window is below
    ``min_depth_usd``, or nothing can be bought.
    """
    if book.best_ask is None:
        return None

    price_cap = min(book.best_ask + max_slippage, max_price)
    window = [level for level in book.asks if level.price <= price_cap]
    if not window:
        return None

    depth_usd_available = sum(level.price * level.size for level in window)
    if depth_usd_available < min_depth_usd:
        return None

    shares = 0.0
    cost = 0.0
    limit_price = window[0].price
    levels_consumed = 0
    for level in window:
        remaining_usd = stake_usd - cost
        if remaining_usd <= 0:
            break
        levels_consumed += 1
        limit_price = level.price
        level_cost = level.price * level.size
        if level_cost <= remaining_usd:
            shares += level.size
            cost += level_cost
        else:
            shares += remaining_usd / level.price
            cost += remaining_usd
            break

    # Floor, never round up: rounding up would quote shares beyond the walked
    # depth / intended stake.
    shares = math.floor(shares * 100) / 100
    if shares <= 0:
        return None

    return SizedOrder(
        limit_price=limit_price,
        shares=shares,
        stake_usd=cost,
        depth_usd_available=depth_usd_available,
        levels_consumed=levels_consumed,
    )
