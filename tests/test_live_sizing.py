"""Tests for depth-aware marketable-limit sizing (polytempo.live.sizing)."""

from __future__ import annotations

import pytest

from polytempo.live.models import BookDepth, BookLevel
from polytempo.live.sizing import size_buy


def _book(*, bids=(), asks=()) -> BookDepth:
    return BookDepth(
        token_id="tok",
        bids=tuple(BookLevel(p, s) for p, s in bids),
        asks=tuple(BookLevel(p, s) for p, s in asks),
        ts_utc="2026-07-18T00:00:00+00:00",
    )


def test_walks_multiple_levels_and_partial_fills_last() -> None:
    book = _book(asks=[(0.50, 100), (0.51, 100), (0.52, 100)])

    sized = size_buy(book, stake_usd=80.0, max_slippage=0.05, min_depth_usd=50.0, max_price=0.90)

    assert sized is not None
    # 100 @ 0.50 = 50, then 30 of budget left -> 30/0.51 shares of the 2nd level.
    assert sized.levels_consumed == 2
    assert sized.limit_price == pytest.approx(0.51)
    assert sized.stake_usd == pytest.approx(80.0)
    assert sized.shares == pytest.approx(round(100 + 30 / 0.51, 2))
    assert sized.depth_usd_available == pytest.approx(50 + 51 + 52)


def test_slippage_cap_excludes_far_levels() -> None:
    book = _book(asks=[(0.50, 100), (0.70, 100)])

    sized = size_buy(book, stake_usd=30.0, max_slippage=0.05, min_depth_usd=40.0, max_price=0.90)

    assert sized is not None
    # Window is only the 0.50 level (0.70 > 0.55 cap); depth reflects just that level.
    assert sized.levels_consumed == 1
    assert sized.limit_price == pytest.approx(0.50)
    assert sized.depth_usd_available == pytest.approx(50.0)
    assert sized.shares == pytest.approx(60.0)  # 30 / 0.50


def test_stake_smaller_than_best_level_partials_it() -> None:
    book = _book(asks=[(0.40, 100)])

    sized = size_buy(book, stake_usd=10.0, max_slippage=0.05, min_depth_usd=40.0, max_price=0.90)

    assert sized is not None
    assert sized.levels_consumed == 1
    assert sized.limit_price == pytest.approx(0.40)
    assert sized.shares == pytest.approx(25.0)  # 10 / 0.40
    assert sized.stake_usd == pytest.approx(10.0)


def test_none_on_thin_depth() -> None:
    book = _book(asks=[(0.50, 10)])  # depth = 5 USD
    assert size_buy(book, 20.0, 0.05, min_depth_usd=50.0, max_price=0.90) is None


def test_none_on_empty_book() -> None:
    assert size_buy(_book(asks=[]), 20.0, 0.05, 0.0, 0.90) is None


def test_none_when_window_empty_from_max_price() -> None:
    book = _book(asks=[(0.60, 100)])
    # max_price below best ask -> price window is empty.
    assert size_buy(book, 20.0, 0.05, 0.0, max_price=0.50) is None


def test_none_when_stake_is_zero() -> None:
    book = _book(asks=[(0.40, 100)])
    assert size_buy(book, 0.0, 0.05, min_depth_usd=0.0, max_price=0.90) is None


def test_stake_exceeding_window_consumes_all_levels() -> None:
    book = _book(asks=[(0.50, 100), (0.55, 100)])

    sized = size_buy(book, stake_usd=1000.0, max_slippage=0.10, min_depth_usd=0.0, max_price=0.90)

    assert sized is not None
    assert sized.levels_consumed == 2
    assert sized.limit_price == pytest.approx(0.55)
    assert sized.shares == pytest.approx(200.0)
    assert sized.stake_usd == pytest.approx(50 + 55)
