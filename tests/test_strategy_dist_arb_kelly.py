"""Tests for the DistArbKellyStrategy fractional-Kelly sizing."""

from polytempo.strategy import DistArbKellyConfig, DistArbKellyStrategy
from polytempo.strategy.edge import BucketEdge


def _edge(
    *,
    label: str,
    probability: float = 0.5,
    yes_bid: float | None = 0.40,
    yes_ask: float | None = 0.45,
    liquidity: float | None = 200.0,
    spread: float | None = None,
) -> BucketEdge:
    return BucketEdge(
        label=label,
        model_probability=probability,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        edge_yes=None,
        edge_yes_pp=(
            (probability - yes_ask) * 100.0 if yes_ask is not None else None
        ),
        edge_no_pp=(
            (yes_bid - probability) * 100.0 if yes_bid is not None else None
        ),
        liquidity_usd=liquidity,
        spread=spread,
    )


def test_buys_with_quarter_kelly_fraction() -> None:
    # YES at p=0.5, ask=0.45: full Kelly = 0.05/0.55, quarter ~ 0.0227.
    [d] = DistArbKellyStrategy().decide([_edge(label="a")])
    assert d.action == "BUY_YES"
    assert d.stake_usd is None
    assert abs(d.stake_fraction - 0.25 * (0.5 - 0.45) / (1 - 0.45)) < 1e-12


def test_fraction_capped_at_ramp_ceiling() -> None:
    # p=0.9 at ask=0.30: quarter Kelly ~ 0.214, capped at 0.05.
    [d] = DistArbKellyStrategy().decide(
        [_edge(label="a", probability=0.9, yes_ask=0.30, yes_bid=0.28)]
    )
    assert d.action == "BUY_YES"
    assert d.stake_fraction == 0.05


def test_no_side_kelly_uses_no_entry_price() -> None:
    # NO at p=0.2, bid=0.40: win 0.8 at entry 0.6, full Kelly = 0.2/0.4.
    [d] = DistArbKellyStrategy().decide(
        [_edge(label="a", probability=0.2, yes_bid=0.40, yes_ask=0.45)]
    )
    assert d.action == "BUY_NO"
    assert d.side == "NO"
    assert d.stake_fraction == 0.05  # quarter Kelly 0.125 capped


def test_selection_matches_dist_arb_gates() -> None:
    # YES edge 0pp, NO edge -5pp: best side does not clear the gate.
    [d] = DistArbKellyStrategy().decide(
        [_edge(label="a", probability=0.45, yes_ask=0.45, yes_bid=0.40)]
    )
    assert d.action == "SKIP"
    assert d.reason == "edge below threshold"
    [d] = DistArbKellyStrategy().decide([_edge(label="a", liquidity=10.0)])
    assert d.action == "SKIP"
    assert d.reason == "liquidity below threshold"


def test_skip_carries_no_fraction() -> None:
    [d] = DistArbKellyStrategy().decide([_edge(label="a", liquidity=10.0)])
    assert d.stake_fraction is None


def test_respects_custom_multiplier_and_cap() -> None:
    strat = DistArbKellyStrategy(
        config=DistArbKellyConfig(kelly_multiplier=1.0, max_stake_fraction=0.50)
    )
    [d] = strat.decide([_edge(label="a")])
    assert abs(d.stake_fraction - (0.5 - 0.45) / (1 - 0.45)) < 1e-12


def test_passes_through_high_spread_warning() -> None:
    [d] = DistArbKellyStrategy().decide([_edge(label="a", spread=0.2)])
    assert d.action == "BUY_YES"
    assert "high spread" in d.warnings


def test_name() -> None:
    assert DistArbKellyStrategy().name == "dist_arb_kelly"
