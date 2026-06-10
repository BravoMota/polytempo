"""Tests for the TailFadeStrategy longshot-bias fade."""

from polytempo.strategy import TailFadeConfig, TailFadeStrategy
from polytempo.strategy.edge import BucketEdge


def _edge(
    *,
    label: str,
    probability: float = 0.01,
    yes_bid: float | None = 0.05,
    liquidity: float | None = 200.0,
    spread: float | None = None,
) -> BucketEdge:
    return BucketEdge(
        label=label,
        model_probability=probability,
        yes_bid=yes_bid,
        yes_ask=None,
        edge_yes=None,
        edge_yes_pp=None,
        edge_no_pp=(
            (yes_bid - probability) * 100.0 if yes_bid is not None else None
        ),
        liquidity_usd=liquidity,
        spread=spread,
    )


def test_fades_overpriced_low_tail() -> None:
    [d] = TailFadeStrategy().decide([_edge(label="13°C or below")])
    assert d.action == "BUY_NO"
    assert d.side == "NO"
    assert d.reason == "tail overpriced against model"
    assert d.edge_yes_pp == 4.0  # NO edge in pp.


def test_fades_high_tail_and_above_kind() -> None:
    [d] = TailFadeStrategy().decide([_edge(label="25°C or higher")])
    assert d.action == "BUY_NO"
    [d] = TailFadeStrategy().decide([_edge(label=">25°C")])
    assert d.action == "BUY_NO"


def test_middle_bucket_is_not_a_tail() -> None:
    [d] = TailFadeStrategy().decide([_edge(label="18°C")])
    assert d.action == "SKIP"
    assert d.reason == "not a tail bucket"


def test_unparseable_label_is_not_a_tail() -> None:
    [d] = TailFadeStrategy().decide([_edge(label="something else")])
    assert d.action == "SKIP"
    assert d.reason == "not a tail bucket"


def test_likely_tail_is_not_faded() -> None:
    [d] = TailFadeStrategy().decide(
        [_edge(label="25°C or higher", probability=0.10)]
    )
    assert d.action == "SKIP"
    assert d.reason == "model probability above threshold"


def test_cheap_tail_is_not_worth_fading() -> None:
    [d] = TailFadeStrategy().decide(
        [_edge(label="25°C or higher", yes_bid=0.01)]
    )
    assert d.action == "SKIP"
    assert d.reason == "yes_bid below threshold"


def test_missing_bid_skips() -> None:
    [d] = TailFadeStrategy().decide(
        [_edge(label="25°C or higher", yes_bid=None)]
    )
    assert d.action == "SKIP"
    assert d.reason == "missing yes_bid"


def test_liquidity_floor_blocks() -> None:
    [d] = TailFadeStrategy().decide(
        [_edge(label="25°C or higher", liquidity=5.0)]
    )
    assert d.action == "SKIP"
    assert d.reason == "liquidity below threshold"


def test_underpriced_tail_is_not_faded() -> None:
    # Bid 0.03 under model probability 0.05: NO edge is negative.
    [d] = TailFadeStrategy().decide(
        [_edge(label="25°C or higher", probability=0.05, yes_bid=0.03)]
    )
    assert d.action == "SKIP"
    assert d.reason == "tail not overpriced"


def test_respects_custom_thresholds() -> None:
    strat = TailFadeStrategy(
        config=TailFadeConfig(max_model_probability=0.15, min_yes_bid=0.01)
    )
    [d] = strat.decide(
        [_edge(label="25°C or higher", probability=0.10, yes_bid=0.12)]
    )
    assert d.action == "BUY_NO"
    [d] = TailFadeStrategy().decide(
        [_edge(label="25°C or higher", probability=0.10, yes_bid=0.12)]
    )
    assert d.action == "SKIP"
    assert d.reason == "model probability above threshold"


def test_passes_through_high_spread_warning() -> None:
    [d] = TailFadeStrategy().decide(
        [_edge(label="25°C or higher", spread=0.2)]
    )
    assert d.action == "BUY_NO"
    assert "high spread" in d.warnings


def test_name() -> None:
    assert TailFadeStrategy().name == "tail_fade"
