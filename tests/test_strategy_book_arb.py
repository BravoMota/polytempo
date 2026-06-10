"""Tests for the BookArbStrategy whole-book sum check."""

from polytempo.strategy import BookArbConfig, BookArbStrategy
from polytempo.strategy.edge import BucketEdge


def _edge(
    *,
    label: str,
    probability: float = 0.3,
    yes_bid: float | None,
    yes_ask: float | None,
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


def _yes_book(asks: list[float]) -> list[BucketEdge]:
    return [
        _edge(label=f"b{i}", yes_bid=a - 0.01, yes_ask=a)
        for i, a in enumerate(asks)
    ]


def test_buys_full_yes_book_when_asks_sum_below_one() -> None:
    decisions = BookArbStrategy().decide(_yes_book([0.30, 0.30, 0.30]))
    assert [d.action for d in decisions] == ["BUY_YES"] * 3
    assert all(d.reason == "book sum arbitrage" for d in decisions)
    assert all(d.edge_yes_pp is not None for d in decisions)


def test_yes_book_holds_equal_shares_per_leg() -> None:
    decisions = BookArbStrategy().decide(_yes_book([0.20, 0.30, 0.40]))
    shares = [d.stake_usd / ask for d, ask in zip(decisions, [0.20, 0.30, 0.40])]
    assert max(shares) - min(shares) < 1e-9
    assert abs(sum(d.stake_usd for d in decisions) - 20.0) < 1e-9


def test_buys_full_no_book_when_bids_sum_above_one() -> None:
    edges = [
        _edge(label=f"b{i}", yes_bid=0.38, yes_ask=0.40) for i in range(3)
    ]
    decisions = BookArbStrategy().decide(edges)
    assert [d.action for d in decisions] == ["BUY_NO"] * 3
    assert all(d.side == "NO" for d in decisions)


def test_skips_when_book_sum_within_noise() -> None:
    decisions = BookArbStrategy().decide(_yes_book([0.33, 0.33, 0.33]))
    assert [d.action for d in decisions] == ["SKIP"] * 3
    assert all(d.reason == "book sum within noise" for d in decisions)


def test_buffer_blocks_marginal_arb() -> None:
    # Asks sum to 0.99: 1pp profit is inside the default 2pp noise buffer.
    decisions = BookArbStrategy().decide(_yes_book([0.33, 0.33, 0.33]))
    assert all(d.action == "SKIP" for d in decisions)
    decisions = BookArbStrategy(
        config=BookArbConfig(min_profit_pp=0.5)
    ).decide(_yes_book([0.33, 0.33, 0.33]))
    assert all(d.action == "BUY_YES" for d in decisions)


def test_incomplete_book_skips() -> None:
    edges = [
        _edge(label="a", yes_bid=None, yes_ask=None),
        _edge(label="b", yes_bid=0.3, yes_ask=0.3),
    ]
    decisions = BookArbStrategy().decide(edges)
    assert all(d.action == "SKIP" for d in decisions)
    assert all(d.reason == "incomplete book" for d in decisions)


def test_single_bucket_is_not_a_book() -> None:
    [d] = BookArbStrategy().decide(_yes_book([0.50]))
    assert d.action == "SKIP"
    assert d.reason == "incomplete book"


def test_thin_book_skips() -> None:
    edges = _yes_book([0.30, 0.30, 0.30])
    edges[1] = _edge(label="b1", yes_bid=0.29, yes_ask=0.30, liquidity=5.0)
    decisions = BookArbStrategy().decide(edges)
    assert all(d.action == "SKIP" for d in decisions)
    assert all(d.reason == "thin book" for d in decisions)


def test_confidence_scales_with_profit() -> None:
    high = BookArbStrategy().decide(_yes_book([0.30, 0.30, 0.30]))
    assert all(d.confidence == "high" for d in high)
    medium = BookArbStrategy().decide(_yes_book([0.32, 0.32, 0.32]))
    assert all(d.confidence == "medium" for d in medium)


def test_empty_edges() -> None:
    assert BookArbStrategy().decide([]) == []


def test_name() -> None:
    assert BookArbStrategy().name == "book_arb"
