"""Tests for the MaxEdgeStrategy wrapper."""

from polytempo.strategy import MaxEdgeStrategy
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


def test_picks_global_best_across_both_sides() -> None:
    edges = [
        _edge(label="a", yes_pp=10.0, no_pp=2.0),  # best YES 10
        _edge(label="b", yes_pp=3.0, no_pp=18.0),  # best NO 18 -> global max
        _edge(label="c", yes_pp=12.0, no_pp=1.0),  # best YES 12
    ]
    decisions = {d.label: d for d in MaxEdgeStrategy().decide(edges)}
    assert decisions["b"].action == "BUY_NO"
    assert decisions["b"].side == "NO"
    assert decisions["b"].edge_yes_pp == 18.0
    assert decisions["a"].action == "SKIP"
    assert decisions["a"].reason == "not max_edge bucket"
    assert decisions["a"].side == "YES"
    assert decisions["a"].edge_yes_pp == 10.0
    assert decisions["c"].action == "SKIP"


def test_best_bucket_below_gate_skips() -> None:
    [d] = MaxEdgeStrategy().decide([_edge(label="a", yes_pp=-1.0, no_pp=-2.0)])
    assert d.action == "SKIP"
    assert d.reason == "edge below threshold"
    assert d.side == "YES"


def test_liquidity_floor_blocks() -> None:
    [d] = MaxEdgeStrategy().decide([_edge(label="a", yes_pp=10.0, liquidity=10.0)])
    assert d.action == "SKIP"
    assert d.reason == "liquidity below threshold"


def test_ties_prefer_yes() -> None:
    [d] = MaxEdgeStrategy().decide([_edge(label="a", yes_pp=10.0, no_pp=10.0)])
    assert d.action == "BUY_YES"
    assert d.side == "YES"


def test_all_none_edges_skips_everything() -> None:
    edges = [
        _edge(label="a", yes_pp=None, no_pp=None),
        _edge(label="b", yes_pp=None, no_pp=None),
    ]
    decisions = MaxEdgeStrategy().decide(edges)
    assert {d.action for d in decisions} == {"SKIP"}
    assert all(d.reason == "not max_edge bucket" for d in decisions)


def test_empty_edges_returns_empty() -> None:
    assert MaxEdgeStrategy().decide([]) == []


def test_name() -> None:
    assert MaxEdgeStrategy().name == "max_edge"
