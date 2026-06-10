"""Tests for the CoverageBandStrategy credible-interval basket."""

from polytempo.strategy import CoverageBandConfig, CoverageBandStrategy
from polytempo.strategy.edge import BucketEdge


def _edge(
    *,
    label: str,
    probability: float,
    yes_ask: float | None = 0.10,
    liquidity: float | None = 200.0,
) -> BucketEdge:
    return BucketEdge(
        label=label,
        model_probability=probability,
        yes_bid=None,
        yes_ask=yes_ask,
        edge_yes=None,
        edge_yes_pp=(
            (probability - yes_ask) * 100.0 if yes_ask is not None else None
        ),
        edge_no_pp=None,
        liquidity_usd=liquidity,
        spread=None,
    )


def _peaked(asks: list[float] | None = None) -> list[BucketEdge]:
    probs = [0.05, 0.15, 0.50, 0.20, 0.10]
    asks = asks if asks is not None else [0.10] * 5
    return [
        _edge(label=f"b{i}", probability=p, yes_ask=a)
        for i, (p, a) in enumerate(zip(probs, asks))
    ]


def test_buys_window_around_peak() -> None:
    # Window grows from the 0.50 peak to {0.15, 0.50, 0.20} = 0.85 mass.
    decisions = CoverageBandStrategy().decide(_peaked())
    assert [d.action for d in decisions] == [
        "SKIP", "BUY_YES", "BUY_YES", "BUY_YES", "SKIP",
    ]
    assert decisions[0].reason == "not in coverage window"
    assert decisions[4].reason == "not in coverage window"


def test_window_legs_use_flat_ticket() -> None:
    decisions = CoverageBandStrategy().decide(_peaked())
    buys = [d for d in decisions if d.action == "BUY_YES"]
    assert all(d.stake_usd == 5.0 for d in buys)
    assert all(d.edge_yes_pp is not None for d in buys)


def test_basket_edge_below_threshold_skips_window() -> None:
    # Mass 0.85 vs cost 0.84: basket edge 1pp is under the 5pp gate.
    decisions = CoverageBandStrategy().decide(
        _peaked(asks=[0.10, 0.28, 0.28, 0.28, 0.10])
    )
    assert all(d.action == "SKIP" for d in decisions)
    assert decisions[2].reason == "basket edge below threshold"
    assert decisions[0].reason == "not in coverage window"


def test_missing_ask_in_window_skips() -> None:
    edges = _peaked()
    edges[2] = _edge(label="b2", probability=0.50, yes_ask=None)
    decisions = CoverageBandStrategy().decide(edges)
    assert all(d.action == "SKIP" for d in decisions)
    assert decisions[2].reason == "missing yes_ask in window"


def test_thin_window_bucket_skips() -> None:
    edges = _peaked()
    edges[1] = _edge(label="b1", probability=0.15, liquidity=5.0)
    decisions = CoverageBandStrategy().decide(edges)
    assert all(d.action == "SKIP" for d in decisions)
    assert decisions[1].reason == "liquidity below threshold"


def test_window_covers_whole_event_when_mass_short() -> None:
    edges = [
        _edge(label=f"b{i}", probability=0.1, yes_ask=0.02) for i in range(4)
    ]
    decisions = CoverageBandStrategy().decide(edges)
    assert all(d.action == "BUY_YES" for d in decisions)


def test_high_confidence_from_basket_edge() -> None:
    decisions = CoverageBandStrategy().decide(_peaked())
    buys = [d for d in decisions if d.action == "BUY_YES"]
    # Basket edge = (0.85 - 0.30) * 100 = 55pp >= 15pp.
    assert all(d.confidence == "high" for d in buys)


def test_respects_custom_target_mass() -> None:
    strat = CoverageBandStrategy(config=CoverageBandConfig(target_mass=0.50))
    decisions = strat.decide(_peaked())
    assert [d.action for d in decisions] == [
        "SKIP", "SKIP", "BUY_YES", "SKIP", "SKIP",
    ]


def test_empty_edges() -> None:
    assert CoverageBandStrategy().decide([]) == []


def test_name() -> None:
    assert CoverageBandStrategy().name == "coverage_band"
