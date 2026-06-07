"""Trade strategy name registry."""

from __future__ import annotations

from polytempo.strategy import ArgmaxYesStrategy, DistArbStrategy, MidBandStrategy
from polytempo.strategy.base import Strategy

_TRADE_STRATEGIES: dict[str, type] = {
    "argmax_yes": ArgmaxYesStrategy,
    "dist_arb": DistArbStrategy,
    "mid_band": MidBandStrategy,
}


def trade_strategy_for_name(name: str) -> Strategy:
    cls = _TRADE_STRATEGIES.get(name)
    if cls is None:
        raise ValueError(
            f"unknown trade_strategy {name!r}; expected one of {sorted(_TRADE_STRATEGIES)}"
        )
    return cls()


def known_trade_strategies() -> tuple[str, ...]:
    return tuple(sorted(_TRADE_STRATEGIES))
