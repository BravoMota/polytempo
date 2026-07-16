"""Trade strategy name registry."""

from __future__ import annotations

from collections.abc import Callable

from polytempo.strategy import (
    ArgmaxNoStrategy,
    ArgmaxYesStrategy,
    BookArbStrategy,
    CoverageBandStrategy,
    DistArbKellyStrategy,
    DistArbStrategy,
    DistArbTightStrategy,
    EdgeBandStrategy,
    MassSideStrategy,
    MaxEdgeStrategy,
    MaxRoiStrategy,
    MidBandStrategy,
    TailFadeStrategy,
    TopKStrategy,
)
from polytempo.strategy.base import Strategy

# Values are zero-arg factories. A bare class is already a zero-arg factory;
# configured variants (e.g. topk_no) use a lambda to pass non-default args.
_TRADE_STRATEGIES: dict[str, Callable[[], Strategy]] = {
    "argmax_yes": ArgmaxYesStrategy,
    "argmax_no": ArgmaxNoStrategy,
    "dist_arb": DistArbStrategy,
    "mid_band": MidBandStrategy,
    "topk_yes": TopKStrategy,
    "topk_no": lambda: TopKStrategy(side="NO", name="topk_no"),
    "max_edge": MaxEdgeStrategy,
    "edge_band": EdgeBandStrategy,
    "book_arb": BookArbStrategy,
    "coverage_band": CoverageBandStrategy,
    "max_roi": MaxRoiStrategy,
    "dist_arb_tight": DistArbTightStrategy,
    "dist_arb_kelly": DistArbKellyStrategy,
    "tail_fade": TailFadeStrategy,
    "mass_side": MassSideStrategy,
}


def trade_strategy_for_name(name: str) -> Strategy:
    factory = _TRADE_STRATEGIES.get(name)
    if factory is None:
        raise ValueError(
            f"unknown trade_strategy {name!r}; expected one of {sorted(_TRADE_STRATEGIES)}"
        )
    return factory()


def known_trade_strategies() -> tuple[str, ...]:
    return tuple(sorted(_TRADE_STRATEGIES))
