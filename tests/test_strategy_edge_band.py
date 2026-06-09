"""Tests for the EdgeBandStrategy wrapper."""

from polytempo.strategy import EdgeBandConfig, EdgeBandStrategy
from polytempo.strategy.edge import BucketEdge


def _edge(
    *,
    label: str,
    yes_pp: float | None = None,
    no_pp: float | None = None,
    liquidity: float | None = 200.0,
    spread: float | None = None,
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
        spread=spread,
    )


def test_buys_richer_side_within_band() -> None:
    [d] = EdgeBandStrategy().decide([_edge(label="a", yes_pp=10.0, no_pp=2.0)])
    assert d.action == "BUY_YES"
    assert d.side == "YES"
    assert d.edge_yes_pp == 10.0
    assert d.confidence == "medium"


def test_high_confidence_within_band() -> None:
    [d] = EdgeBandStrategy().decide([_edge(label="a", yes_pp=20.0)])
    assert d.action == "BUY_YES"
    assert d.confidence == "high"


def test_below_band_skips() -> None:
    [d] = EdgeBandStrategy().decide([_edge(label="a", yes_pp=3.0)])
    assert d.action == "SKIP"
    assert d.reason == "edge below band"


def test_above_band_skips_as_implausible() -> None:
    [d] = EdgeBandStrategy().decide([_edge(label="a", yes_pp=40.0)])
    assert d.action == "SKIP"
    assert d.reason == "edge above band"


def test_max_boundary_is_inclusive() -> None:
    [d] = EdgeBandStrategy().decide([_edge(label="a", yes_pp=25.0)])
    assert d.action == "BUY_YES"


def test_min_boundary_is_exclusive() -> None:
    [d] = EdgeBandStrategy().decide([_edge(label="a", yes_pp=5.0)])
    assert d.action == "SKIP"
    assert d.reason == "edge below band"


def test_picks_no_side() -> None:
    [d] = EdgeBandStrategy().decide([_edge(label="a", yes_pp=2.0, no_pp=12.0)])
    assert d.action == "BUY_NO"
    assert d.side == "NO"
    assert d.edge_yes_pp == 12.0


def test_liquidity_floor_blocks() -> None:
    [d] = EdgeBandStrategy().decide([_edge(label="a", yes_pp=10.0, liquidity=10.0)])
    assert d.action == "SKIP"
    assert d.reason == "liquidity below threshold"


def test_missing_edge_skips() -> None:
    [d] = EdgeBandStrategy().decide([_edge(label="a", yes_pp=None, no_pp=None)])
    assert d.action == "SKIP"
    assert d.reason == "missing edge"
    assert d.side == "YES"


def test_respects_custom_band() -> None:
    strat = EdgeBandStrategy(config=EdgeBandConfig(min_edge_pp=1.0, max_edge_pp=8.0))
    [d] = strat.decide([_edge(label="a", yes_pp=10.0)])
    assert d.action == "SKIP"
    assert d.reason == "edge above band"


def test_passes_through_high_spread_warning() -> None:
    [d] = EdgeBandStrategy().decide([_edge(label="a", yes_pp=10.0, spread=0.2)])
    assert d.action == "BUY_YES"
    assert "high spread" in d.warnings


def test_name() -> None:
    assert EdgeBandStrategy().name == "edge_band"
