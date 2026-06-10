"""Tests for the MaxRoiStrategy return-on-stake ranking."""

from polytempo.strategy import MaxRoiConfig, MaxRoiStrategy
from polytempo.strategy.edge import BucketEdge


def _edge(
    *,
    label: str,
    probability: float,
    yes_bid: float | None = None,
    yes_ask: float | None = None,
    liquidity: float | None = 200.0,
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
        spread=None,
    )


def test_prefers_high_roi_over_high_pp_edge() -> None:
    # a: 5pp edge at 5 cents = 100% ROI; b: 10pp edge at 45 cents = 22% ROI.
    edges = [
        _edge(label="a", probability=0.10, yes_ask=0.05),
        _edge(label="b", probability=0.55, yes_ask=0.45),
    ]
    a, b = MaxRoiStrategy().decide(edges)
    assert a.action == "BUY_YES"
    assert b.action == "SKIP"
    assert b.reason == "not max_roi bucket"


def test_picks_no_side_roi() -> None:
    # NO wins with probability 0.95 at entry 0.90: ROI ~5.6%.
    [d] = MaxRoiStrategy().decide(
        [_edge(label="a", probability=0.05, yes_bid=0.10, yes_ask=0.99)]
    )
    assert d.action == "BUY_NO"
    assert d.side == "NO"
    assert d.edge_yes_pp == 5.0  # NO edge in pp, dist_arb convention.


def test_win_probability_floor_blocks_longshots() -> None:
    # 1-cent bucket with 2% model probability has 100% ROI but no floor pass.
    edges = [_edge(label="a", probability=0.02, yes_ask=0.01)]
    [d] = MaxRoiStrategy().decide(edges)
    assert d.action == "SKIP"
    assert d.reason == "not max_roi bucket"


def test_roi_below_threshold_skips() -> None:
    # ROI = 0.50/0.49 - 1 ~ 2%, under the 5% gate.
    [d] = MaxRoiStrategy().decide(
        [_edge(label="a", probability=0.50, yes_ask=0.49)]
    )
    assert d.action == "SKIP"
    assert d.reason == "roi below threshold"


def test_liquidity_floor_blocks() -> None:
    [d] = MaxRoiStrategy().decide(
        [_edge(label="a", probability=0.10, yes_ask=0.05, liquidity=5.0)]
    )
    assert d.action == "SKIP"
    assert d.reason == "liquidity below threshold"


def test_one_ticket_per_event() -> None:
    edges = [
        _edge(label=f"b{i}", probability=0.20, yes_ask=0.10) for i in range(4)
    ]
    decisions = MaxRoiStrategy().decide(edges)
    assert sum(d.action == "BUY_YES" for d in decisions) == 1


def test_confidence_scales_with_roi() -> None:
    [d] = MaxRoiStrategy().decide(
        [_edge(label="a", probability=0.10, yes_ask=0.05)]
    )
    assert d.confidence == "high"
    [d] = MaxRoiStrategy().decide(
        [_edge(label="a", probability=0.55, yes_ask=0.50)]
    )
    assert d.confidence == "medium"


def test_respects_custom_floors() -> None:
    strat = MaxRoiStrategy(config=MaxRoiConfig(min_win_probability=0.01))
    [d] = strat.decide([_edge(label="a", probability=0.02, yes_ask=0.01)])
    assert d.action == "BUY_YES"


def test_missing_quotes_skip() -> None:
    [d] = MaxRoiStrategy().decide([_edge(label="a", probability=0.5)])
    assert d.action == "SKIP"
    assert d.reason == "not max_roi bucket"


def test_empty_edges() -> None:
    assert MaxRoiStrategy().decide([]) == []


def test_name() -> None:
    assert MaxRoiStrategy().name == "max_roi"
