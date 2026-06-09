"""Tests for the TopKStrategy wrapper."""

import pytest

from polytempo.strategy import TopKStrategy
from polytempo.strategy.decision import DecisionConfig
from polytempo.strategy.edge import BucketEdge


def _edge(
    *,
    label: str,
    yes_pp: float | None = None,
    no_pp: float | None = None,
    liquidity: float | None = 200.0,
) -> BucketEdge:
    return BucketEdge(
        label=label,
        model_probability=0.5,
        yes_bid=0.4,
        yes_ask=0.45,
        edge_yes=None,
        edge_yes_pp=yes_pp,
        edge_no_pp=no_pp,
        liquidity_usd=liquidity,
        spread=None,
    )


def test_buys_the_three_highest_yes_edges() -> None:
    edges = [
        _edge(label="a", yes_pp=2.0),
        _edge(label="b", yes_pp=20.0),
        _edge(label="c", yes_pp=8.0),
        _edge(label="d", yes_pp=15.0),
        _edge(label="e", yes_pp=1.0),
    ]
    decisions = {d.label: d for d in TopKStrategy().decide(edges)}  # default k=3
    assert decisions["b"].action == "BUY_YES"
    assert decisions["d"].action == "BUY_YES"
    assert decisions["c"].action == "BUY_YES"
    assert decisions["a"].action == "SKIP"
    assert decisions["a"].reason == "not in top-k"
    assert decisions["e"].action == "SKIP"


def test_respects_custom_k() -> None:
    strat = TopKStrategy(k=1)
    edges = [_edge(label="a", yes_pp=5.0), _edge(label="b", yes_pp=10.0)]
    decisions = {d.label: d for d in strat.decide(edges)}
    assert decisions["b"].action == "BUY_YES"
    assert decisions["a"].action == "SKIP"
    assert decisions["a"].reason == "not in top-k"


def test_chosen_bucket_below_edge_gate_skips_via_gate() -> None:
    strat = TopKStrategy(k=2)
    edges = [_edge(label="a", yes_pp=10.0), _edge(label="b", yes_pp=-1.0)]
    decisions = {d.label: d for d in strat.decide(edges)}
    assert decisions["a"].action == "BUY_YES"
    # b is within top-2 but fails the edge gate.
    assert decisions["b"].action == "SKIP"
    assert decisions["b"].reason == "edge below threshold"


def test_liquidity_floor_blocks_chosen_bucket() -> None:
    [d] = TopKStrategy(k=1).decide([_edge(label="a", yes_pp=10.0, liquidity=10.0)])
    assert d.action == "SKIP"
    assert d.reason == "liquidity below threshold"


def test_respects_custom_threshold() -> None:
    strat = TopKStrategy(k=1, config=DecisionConfig(min_edge_pp=12.0))
    [d] = strat.decide([_edge(label="a", yes_pp=10.0)])
    assert d.action == "SKIP"
    assert d.reason == "edge below threshold"


def test_ignores_none_edges_when_ranking() -> None:
    edges = [
        _edge(label="no_quote", yes_pp=None),
        _edge(label="a", yes_pp=3.0),
    ]
    decisions = {d.label: d for d in TopKStrategy(k=1).decide(edges)}
    assert decisions["a"].action == "BUY_YES"
    assert decisions["no_quote"].action == "SKIP"
    assert decisions["no_quote"].reason == "not in top-k"
    assert decisions["no_quote"].edge_yes_pp is None


def test_no_side_ranks_no_edge() -> None:
    strat = TopKStrategy(k=1, side="NO", name="topk_no")
    edges = [_edge(label="a", no_pp=5.0), _edge(label="b", no_pp=12.0)]
    decisions = {d.label: d for d in strat.decide(edges)}
    assert decisions["b"].action == "BUY_NO"
    assert decisions["b"].side == "NO"
    assert decisions["a"].action == "SKIP"
    assert decisions["a"].side == "NO"
    assert decisions["a"].reason == "not in top-k"


def test_invalid_side_raises() -> None:
    with pytest.raises(ValueError):
        TopKStrategy(side="MAYBE").decide([_edge(label="a", yes_pp=5.0)])


def test_empty_edges_returns_empty() -> None:
    assert TopKStrategy().decide([]) == []


def test_default_name() -> None:
    assert TopKStrategy().name == "topk_yes"
