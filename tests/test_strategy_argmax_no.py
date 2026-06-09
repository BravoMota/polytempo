"""Tests for the ArgmaxNoStrategy wrapper."""

from polytempo.strategy import ArgmaxNoStrategy
from polytempo.strategy.decision import DecisionConfig
from polytempo.strategy.edge import BucketEdge


def _edge(
    edge_no_pp: float | None,
    *,
    label: str = "b",
    liquidity: float | None = 200.0,
) -> BucketEdge:
    return BucketEdge(
        label=label,
        model_probability=0.5,
        yes_bid=0.4,
        yes_ask=0.45,
        edge_yes=None,
        edge_yes_pp=None,
        edge_no_pp=edge_no_pp,
        liquidity_usd=liquidity,
        spread=None,
    )


def test_default_strategy_buys_argmax_no_when_edge_passes() -> None:
    [d] = ArgmaxNoStrategy().decide([_edge(10.0)])
    assert d.action == "BUY_NO"
    assert d.side == "NO"
    assert d.edge_yes_pp == 10.0


def test_strategy_respects_custom_threshold() -> None:
    strat = ArgmaxNoStrategy(config=DecisionConfig(min_edge_pp=12.0))
    [d] = strat.decide([_edge(10.0)])
    assert d.action == "SKIP"
    assert d.reason == "edge below threshold"


def test_only_highest_no_edge_bucket_can_buy() -> None:
    edges = [
        _edge(5.0, label="low"),
        _edge(20.0, label="best_no"),
        _edge(12.0, label="mid"),
    ]
    decisions = {d.label: d for d in ArgmaxNoStrategy().decide(edges)}
    assert decisions["best_no"].action == "BUY_NO"
    assert decisions["low"].action == "SKIP"
    assert decisions["mid"].action == "SKIP"
    assert decisions["low"].reason == "not argmax_no bucket"
    assert decisions["low"].side == "NO"


def test_argmax_no_fails_gate_skips_everything() -> None:
    edges = [
        _edge(-1.0, label="best_no_negative"),
        _edge(-5.0, label="worse"),
    ]
    actions = {d.label: d.action for d in ArgmaxNoStrategy().decide(edges)}
    assert actions == {"best_no_negative": "SKIP", "worse": "SKIP"}


def test_liquidity_floor_blocks_argmax_no() -> None:
    [d] = ArgmaxNoStrategy().decide([_edge(20.0, liquidity=10.0)])
    assert d.action == "SKIP"
    assert d.reason == "liquidity below threshold"


def test_buckets_without_no_edge_are_skipped() -> None:
    # No bucket has a computable NO edge -> nothing to argmax over.
    edges = [_edge(None, label="a"), _edge(None, label="b")]
    decisions = ArgmaxNoStrategy().decide(edges)
    assert {d.action for d in decisions} == {"SKIP"}
    assert all(d.reason == "not argmax_no bucket" for d in decisions)


def test_argmax_no_ignores_none_edge_when_picking() -> None:
    edges = [
        _edge(None, label="no_quote"),
        _edge(8.0, label="best_no"),
    ]
    decisions = {d.label: d for d in ArgmaxNoStrategy().decide(edges)}
    assert decisions["best_no"].action == "BUY_NO"
    assert decisions["no_quote"].action == "SKIP"


def test_empty_edges_returns_empty() -> None:
    assert ArgmaxNoStrategy().decide([]) == []


def test_strategy_has_name() -> None:
    assert ArgmaxNoStrategy().name == "argmax_no"
