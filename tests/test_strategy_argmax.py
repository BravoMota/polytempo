"""Tests for the ArgmaxYesStrategy wrapper."""

from polytempo.strategy import ArgmaxYesStrategy
from polytempo.strategy.decision import DecisionConfig
from polytempo.strategy.edge import BucketEdge


def _edge(edge_pp: float | None, liquidity: float | None = 200.0) -> BucketEdge:
    return BucketEdge(
        label="b",
        model_probability=0.5,
        yes_bid=0.4,
        yes_ask=0.35,
        edge_yes=0.15,
        edge_yes_pp=edge_pp,
        liquidity_usd=liquidity,
        spread=None,
    )


def test_default_strategy_buys_when_edge_passes() -> None:
    strat = ArgmaxYesStrategy()
    [d] = strat.decide([_edge(10.0)])
    assert d.action == "BUY_YES"


def test_strategy_respects_custom_threshold() -> None:
    strat = ArgmaxYesStrategy(config=DecisionConfig(min_edge_pp=12.0))
    [d] = strat.decide([_edge(10.0)])
    assert d.action == "SKIP"


def test_strategy_preserves_input_order() -> None:
    strat = ArgmaxYesStrategy()
    edges = [_edge(10.0), _edge(-1.0), _edge(15.0)]
    actions = [d.action for d in strat.decide(edges)]
    assert actions == ["BUY_YES", "SKIP", "BUY_YES"]


def test_strategy_has_name() -> None:
    assert ArgmaxYesStrategy().name == "argmax_yes"
