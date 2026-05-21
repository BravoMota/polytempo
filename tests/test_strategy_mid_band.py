"""Tests for MidBandStrategy."""

from polytempo.strategy import MidBandConfig, MidBandStrategy
from polytempo.strategy.edge import BucketEdge


def _edge(
    *,
    yes_ask: float | None = 0.40,
    edge_pp: float | None = 5.0,
    liquidity: float | None = 100.0,
) -> BucketEdge:
    return BucketEdge(
        label="b",
        model_probability=0.5,
        yes_bid=0.35,
        yes_ask=yes_ask,
        edge_yes=None if edge_pp is None else edge_pp / 100.0,
        edge_yes_pp=edge_pp,
        edge_no_pp=None,
        liquidity_usd=liquidity,
        spread=None,
    )


def test_buys_inside_band_with_flat_ticket() -> None:
    [d] = MidBandStrategy().decide([_edge(yes_ask=0.40, edge_pp=5.0)])
    assert d.action == "BUY_YES"
    assert d.side == "YES"
    assert d.stake_usd == 5.0


def test_skip_below_band() -> None:
    [d] = MidBandStrategy().decide([_edge(yes_ask=0.15, edge_pp=5.0)])
    assert d.action == "SKIP"
    assert d.reason == "price outside band"


def test_skip_above_band() -> None:
    [d] = MidBandStrategy().decide([_edge(yes_ask=0.75, edge_pp=5.0)])
    assert d.action == "SKIP"
    assert d.reason == "price outside band"


def test_band_endpoints_inclusive() -> None:
    [low] = MidBandStrategy().decide([_edge(yes_ask=0.20, edge_pp=5.0)])
    [high] = MidBandStrategy().decide([_edge(yes_ask=0.60, edge_pp=5.0)])
    assert low.action == "BUY_YES"
    assert high.action == "BUY_YES"


def test_skip_when_yes_ask_missing() -> None:
    [d] = MidBandStrategy().decide([_edge(yes_ask=None, edge_pp=5.0)])
    assert d.action == "SKIP"
    assert d.reason == "missing yes_ask"


def test_skip_when_edge_not_positive() -> None:
    [d] = MidBandStrategy().decide([_edge(yes_ask=0.40, edge_pp=0.0)])
    assert d.action == "SKIP"
    assert d.reason == "edge below threshold"


def test_skip_when_liquidity_below_floor() -> None:
    [d] = MidBandStrategy().decide([_edge(yes_ask=0.40, edge_pp=5.0, liquidity=10.0)])
    assert d.action == "SKIP"
    assert d.reason == "liquidity below threshold"


def test_flat_ticket_is_configurable() -> None:
    cfg = MidBandConfig(flat_ticket_usd=10.0)
    [d] = MidBandStrategy(config=cfg).decide([_edge(yes_ask=0.40, edge_pp=5.0)])
    assert d.stake_usd == 10.0
