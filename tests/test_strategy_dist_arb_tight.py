"""Tests for the DistArbTightStrategy spread-gated wrapper."""

from polytempo.strategy import DistArbTightConfig, DistArbTightStrategy
from polytempo.strategy.edge import BucketEdge


def _edge(
    *,
    label: str,
    yes_pp: float | None = None,
    no_pp: float | None = None,
    liquidity: float | None = 200.0,
    spread: float | None = 0.03,
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


def test_buys_richer_side_on_tight_quote() -> None:
    [d] = DistArbTightStrategy().decide([_edge(label="a", yes_pp=10.0, no_pp=2.0)])
    assert d.action == "BUY_YES"
    assert d.side == "YES"
    assert d.edge_yes_pp == 10.0


def test_missing_spread_blocks() -> None:
    [d] = DistArbTightStrategy().decide([_edge(label="a", yes_pp=10.0, spread=None)])
    assert d.action == "SKIP"
    assert d.reason == "missing spread"


def test_wide_spread_blocks() -> None:
    [d] = DistArbTightStrategy().decide([_edge(label="a", yes_pp=10.0, spread=0.08)])
    assert d.action == "SKIP"
    assert d.reason == "spread above threshold"


def test_max_spread_boundary_is_inclusive() -> None:
    [d] = DistArbTightStrategy().decide([_edge(label="a", yes_pp=10.0, spread=0.05)])
    assert d.action == "BUY_YES"


def test_higher_liquidity_floor_blocks() -> None:
    # 50 passes dist_arb's floor of 25 but not the tight floor of 100.
    [d] = DistArbTightStrategy().decide([_edge(label="a", yes_pp=10.0, liquidity=50.0)])
    assert d.action == "SKIP"
    assert d.reason == "liquidity below threshold"


def test_picks_no_side() -> None:
    [d] = DistArbTightStrategy().decide([_edge(label="a", yes_pp=2.0, no_pp=12.0)])
    assert d.action == "BUY_NO"
    assert d.side == "NO"
    assert d.edge_yes_pp == 12.0


def test_edge_below_threshold_skips() -> None:
    [d] = DistArbTightStrategy().decide([_edge(label="a", yes_pp=-1.0)])
    assert d.action == "SKIP"
    assert d.reason == "edge below threshold"


def test_missing_edge_skips() -> None:
    [d] = DistArbTightStrategy().decide([_edge(label="a")])
    assert d.action == "SKIP"
    assert d.reason == "missing edge"


def test_respects_custom_spread_gate() -> None:
    strat = DistArbTightStrategy(config=DistArbTightConfig(max_spread=0.10))
    [d] = strat.decide([_edge(label="a", yes_pp=10.0, spread=0.08)])
    assert d.action == "BUY_YES"


def test_name() -> None:
    assert DistArbTightStrategy().name == "dist_arb_tight"
