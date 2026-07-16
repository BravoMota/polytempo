"""Tests for MassSideStrategy distribution-directed side selection."""

from polytempo.strategy import MassSideConfig, MassSideStrategy
from polytempo.strategy.edge import BucketEdge


def _edge(
    *,
    label: str,
    probability: float,
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


def test_name() -> None:
    assert MassSideStrategy().name == "mass_side"


def test_core_buys_yes_never_no_even_when_no_edge_larger() -> None:
    # Mode at 20°C. Market overprices YES so NO edge is huge — still YES-only.
    edges = [
        _edge(label="18°C", probability=0.05, yes_bid=0.08, yes_ask=0.10),
        # YES edge 5pp, NO edge 15pp — still must BUY_YES (side locked by mass).
        _edge(label="20°C", probability=0.40, yes_bid=0.55, yes_ask=0.35),
        _edge(label="22°C", probability=0.10, yes_bid=0.12, yes_ask=0.15),
    ]
    decisions = MassSideStrategy().decide(edges)
    by_label = {d.label: d for d in decisions}
    assert by_label["20°C"].action == "BUY_YES"
    assert by_label["20°C"].side == "YES"
    assert by_label["20°C"].stake_usd is None  # ledger ramp
    assert by_label["20°C"].reason == "core mass yes"


def test_core_skips_when_yes_ask_too_expensive() -> None:
    edges = [
        _edge(label="20°C", probability=0.40, yes_bid=0.88, yes_ask=0.92),
        _edge(label="21°C", probability=0.10, yes_bid=0.10, yes_ask=0.12),
    ]
    [mode, _] = MassSideStrategy().decide(edges)
    assert mode.action == "SKIP"
    assert mode.reason == "yes_ask above max"


def test_fade_buys_no_not_cheap_yes() -> None:
    # Low-mass bucket with cheap YES ask — must fade NO, not lottery YES.
    edges = [
        _edge(label="20°C", probability=0.50, yes_bid=0.45, yes_ask=0.48),
        # Cheap YES ask (0.04) must not trigger YES; NO at 0.80 clears max_no_price.
        _edge(label="29°C or higher", probability=0.02, yes_bid=0.20, yes_ask=0.04),
    ]
    decisions = MassSideStrategy().decide(edges)
    by_label = {d.label: d for d in decisions}
    assert by_label["29°C or higher"].action == "BUY_NO"
    assert by_label["29°C or higher"].side == "NO"
    assert by_label["29°C or higher"].reason == "fade low-mass no"


def test_fade_skips_when_no_price_too_high() -> None:
    edges = [
        _edge(label="20°C", probability=0.50, yes_bid=0.45, yes_ask=0.48),
        # yes_bid=0.05 → NO costs 0.95 > max_no_price 0.85
        _edge(label="tail", probability=0.01, yes_bid=0.05, yes_ask=0.06),
    ]
    [_, fade] = MassSideStrategy().decide(edges)
    assert fade.action == "SKIP"
    assert fade.reason == "no price above max"


def test_believe_tail_buys_yes_with_one_dollar_floor() -> None:
    edges = [
        _edge(label="20°C", probability=0.40, yes_bid=0.35, yes_ask=0.38),
        # p=0.10 >= p_believe, below core_floor (0.5*0.40=0.20)
        _edge(label="25°C", probability=0.10, yes_bid=0.05, yes_ask=0.06),
    ]
    [_, tail] = MassSideStrategy().decide(edges)
    assert tail.action == "BUY_YES"
    assert tail.side == "YES"
    assert tail.stake_usd == 1.0
    assert tail.reason == "believed-tail yes"


def test_mid_mass_ignored() -> None:
    edges = [
        _edge(label="20°C", probability=0.40, yes_bid=0.35, yes_ask=0.38),
        # p=0.06: above p_fade (0.05), below p_believe (0.08), below core
        _edge(label="23°C", probability=0.06, yes_bid=0.10, yes_ask=0.12),
    ]
    [_, mid] = MassSideStrategy().decide(edges)
    assert mid.action == "SKIP"
    assert mid.reason == "mass role ignore"


def test_respects_custom_believe_stake() -> None:
    strat = MassSideStrategy(config=MassSideConfig(believe_tail_stake_usd=2.5))
    edges = [
        _edge(label="20°C", probability=0.40, yes_bid=0.35, yes_ask=0.38),
        _edge(label="25°C", probability=0.10, yes_bid=0.05, yes_ask=0.06),
    ]
    [_, tail] = strat.decide(edges)
    assert tail.stake_usd == 2.5


def test_passes_through_high_spread_warning() -> None:
    edges = [
        _edge(
            label="20°C",
            probability=0.40,
            yes_bid=0.35,
            yes_ask=0.38,
            spread=0.2,
        ),
    ]
    [d] = MassSideStrategy().decide(edges)
    assert d.action == "BUY_YES"
    assert "high spread" in d.warnings
