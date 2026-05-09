"""Tests for deterministic decision rules."""

from polytempo.strategy.decision import DecisionConfig, decide_all, decide_bucket
from polytempo.strategy.edge import BucketEdge


def _edge(
    *,
    label: str = "b",
    edge_yes_pp: float | None = 10.0,
    liquidity_usd: float | None = 200.0,
    spread: float | None = None,
) -> BucketEdge:
    return BucketEdge(
        label=label,
        model_probability=0.5,
        yes_bid=0.4,
        yes_ask=0.35,
        edge_yes=0.15,
        edge_yes_pp=edge_yes_pp,
        liquidity_usd=liquidity_usd,
        spread=spread,
    )


def test_buy_yes_when_edge_and_liquidity_pass() -> None:
    d = decide_bucket(_edge(edge_yes_pp=10.0, liquidity_usd=200.0))
    assert d.action == "BUY_YES"
    assert d.reason == "edge and liquidity rules passed"
    assert d.confidence == "medium"


def test_skip_missing_edge() -> None:
    d = decide_bucket(_edge(edge_yes_pp=None))
    assert d.action == "SKIP"
    assert d.reason == "missing edge"


def test_skip_edge_below_threshold() -> None:
    d = decide_bucket(_edge(edge_yes_pp=5.0))
    assert d.action == "SKIP"
    assert d.reason == "edge below threshold"


def test_skip_missing_liquidity() -> None:
    d = decide_bucket(_edge(edge_yes_pp=10.0, liquidity_usd=None))
    assert d.action == "SKIP"
    assert d.reason == "missing liquidity"


def test_skip_liquidity_too_low() -> None:
    d = decide_bucket(_edge(edge_yes_pp=10.0, liquidity_usd=50.0))
    assert d.action == "SKIP"
    assert d.reason == "liquidity below threshold"


def test_high_spread_warns_but_does_not_block_buy() -> None:
    d = decide_bucket(_edge(edge_yes_pp=10.0, liquidity_usd=200.0, spread=0.15))
    assert d.action == "BUY_YES"
    assert "high spread" in d.warnings


def test_no_warning_when_spread_missing() -> None:
    d = decide_bucket(_edge(spread=None))
    assert d.warnings == []


def test_no_warning_when_spread_below_threshold() -> None:
    d = decide_bucket(_edge(spread=0.05))
    assert d.warnings == []


def test_high_confidence_when_edge_at_least_twice_threshold() -> None:
    d = decide_bucket(_edge(edge_yes_pp=14.0))
    assert d.action == "BUY_YES"
    assert d.confidence == "high"


def test_medium_confidence_buy_yes_below_twice_threshold() -> None:
    d = decide_bucket(_edge(edge_yes_pp=13.9))
    assert d.action == "BUY_YES"
    assert d.confidence == "medium"


def test_decide_all_preserves_order() -> None:
    cfg = DecisionConfig(min_edge_pp=5.0)
    edges = [
        _edge(label="first", edge_yes_pp=10.0, liquidity_usd=200.0),
        _edge(label="second", edge_yes_pp=10.0, liquidity_usd=200.0),
    ]
    out = decide_all(edges, cfg)
    assert [x.label for x in out] == ["first", "second"]
