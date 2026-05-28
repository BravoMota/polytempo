"""Tests for the ArgmaxYesStrategy wrapper."""

from polytempo.strategy import ArgmaxYesStrategy
from polytempo.strategy.decision import DecisionConfig
from polytempo.strategy.edge import BucketEdge


def _edge(
    edge_pp: float | None,
    *,
    label: str = "b",
    probability: float = 0.5,
    liquidity: float | None = 200.0,
) -> BucketEdge:
    return BucketEdge(
        label=label,
        model_probability=probability,
        yes_bid=0.4,
        yes_ask=0.35,
        edge_yes=0.15,
        edge_yes_pp=edge_pp,
        edge_no_pp=None,
        liquidity_usd=liquidity,
        spread=None,
    )


def test_default_strategy_buys_argmax_when_edge_passes() -> None:
    strat = ArgmaxYesStrategy()
    [d] = strat.decide([_edge(10.0)])
    assert d.action == "BUY_YES"


def test_strategy_respects_custom_threshold() -> None:
    strat = ArgmaxYesStrategy(config=DecisionConfig(min_edge_pp=12.0))
    [d] = strat.decide([_edge(10.0)])
    assert d.action == "SKIP"


def test_only_argmax_bucket_can_buy() -> None:
    strat = ArgmaxYesStrategy()
    edges = [
        _edge(10.0, label="low", probability=0.1),
        _edge(15.0, label="argmax", probability=0.7),
        _edge(20.0, label="mid", probability=0.2),
    ]
    actions = {d.label: d.action for d in strat.decide(edges)}
    assert actions == {"low": "SKIP", "argmax": "BUY_YES", "mid": "SKIP"}


def test_non_argmax_skip_reason() -> None:
    strat = ArgmaxYesStrategy()
    edges = [
        _edge(20.0, label="hi_edge_low_prob", probability=0.1),
        _edge(5.0, label="argmax", probability=0.6),
    ]
    decisions = {d.label: d for d in strat.decide(edges)}
    assert decisions["hi_edge_low_prob"].action == "SKIP"
    assert decisions["hi_edge_low_prob"].reason == "not argmax bucket"
    assert decisions["argmax"].action == "BUY_YES"


def test_argmax_fails_gate_skips_everything() -> None:
    strat = ArgmaxYesStrategy()
    edges = [
        _edge(-1.0, label="argmax_no_edge", probability=0.7),
        _edge(20.0, label="positive_edge", probability=0.3),
    ]
    actions = {d.label: d.action for d in strat.decide(edges)}
    assert actions == {"argmax_no_edge": "SKIP", "positive_edge": "SKIP"}


def test_empty_edges_returns_empty() -> None:
    assert ArgmaxYesStrategy().decide([]) == []


def test_strategy_has_name() -> None:
    assert ArgmaxYesStrategy().name == "argmax_yes"
