"""Tests for the execution clients (polytempo.live.execution).

No network and no ``py-clob-client`` requirement: the dry-run client is fully
in-memory and ``normalize_clob_status`` is a module-level pure function.
"""

from __future__ import annotations

import importlib.util

import pytest

from polytempo.live.config import LiveCredentials
from polytempo.live.execution import (
    DryRunExecutionClient,
    PyClobExecutionClient,
    normalize_clob_status,
)
from polytempo.live.models import (
    MODE_DRY_RUN,
    SIDE_YES,
    STATE_CANCELED,
    STATE_FAILED,
    STATE_FILLED,
    STATE_OPEN,
    STATE_PENDING,
    STATE_REJECTED,
    STATE_SUBMITTED,
    BookDepth,
    BookLevel,
    OrderIntent,
    assert_transition,
)


def _book(token_id: str, *, asks=()) -> BookDepth:
    return BookDepth(
        token_id=token_id,
        bids=(),
        asks=tuple(BookLevel(p, s) for p, s in asks),
        ts_utc="2026-07-18T00:00:00+00:00",
    )


def _intent(token_id: str, shares: float, limit_price: float) -> OrderIntent:
    return OrderIntent(
        intent_id=f"i-{token_id}",
        event_id="evt",
        bucket_label="26°C",
        token_id=token_id,
        market_side=SIDE_YES,
        limit_price=limit_price,
        shares=shares,
        stake_usd=shares * limit_price,
        knob_id="knob",
        mode=MODE_DRY_RUN,
        ts_utc="2026-07-18T00:00:00+00:00",
    )


# ── DryRunExecutionClient ────────────────────────────────────────────────────────
def test_dry_run_full_fill_updates_position_and_balance() -> None:
    books = {"tok": _book("tok", asks=[(0.50, 100)])}
    client = DryRunExecutionClient(lambda t: books.get(t), starting_balance_usd=100.0)

    placed = client.place_limit_buy(_intent("tok", 50.0, 0.55))

    assert placed.state == STATE_FILLED
    status = client.order_status(placed.order_id)
    assert status.state == STATE_FILLED
    assert status.filled_shares == pytest.approx(50.0)
    assert status.avg_fill_price == pytest.approx(0.50)
    assert client.collateral_balance_usd() == pytest.approx(75.0)  # 100 - 50*0.50
    assert client.open_orders() == []

    pos = client.positions()
    assert len(pos) == 1
    assert pos[0].token_id == "tok"
    assert pos[0].shares == pytest.approx(50.0)
    assert pos[0].avg_price == pytest.approx(0.50)

    # Cancel on a filled (terminal) order is a no-op.
    assert client.cancel_order(placed.order_id) is False


def test_dry_run_partial_fill_then_cancel_keeps_filled() -> None:
    books = {"tok": _book("tok", asks=[(0.50, 30)])}
    client = DryRunExecutionClient(lambda t: books.get(t), starting_balance_usd=100.0)

    placed = client.place_limit_buy(_intent("tok", 50.0, 0.55))

    assert placed.state == STATE_OPEN
    status = client.order_status(placed.order_id)
    assert status.filled_shares == pytest.approx(30.0)
    assert status.avg_fill_price == pytest.approx(0.50)
    assert client.collateral_balance_usd() == pytest.approx(85.0)  # 100 - 30*0.50
    assert len(client.open_orders()) == 1

    assert client.cancel_order(placed.order_id) is True
    canceled = client.order_status(placed.order_id)
    assert canceled.state == STATE_CANCELED
    assert canceled.filled_shares == pytest.approx(30.0)  # partial fill preserved
    assert client.open_orders() == []
    assert client.cancel_order(placed.order_id) is False  # second cancel is a no-op


def test_dry_run_no_book_rests_open_with_no_fill() -> None:
    client = DryRunExecutionClient(lambda t: None, starting_balance_usd=100.0)

    placed = client.place_limit_buy(_intent("tok", 50.0, 0.55))

    assert placed.state == STATE_OPEN
    assert client.order_status(placed.order_id).filled_shares == pytest.approx(0.0)
    assert client.collateral_balance_usd() == pytest.approx(100.0)
    assert client.positions() == []


def test_dry_run_averages_position_across_two_buys() -> None:
    books = {"tok": _book("tok", asks=[(0.50, 30)])}
    client = DryRunExecutionClient(lambda t: books.get(t), starting_balance_usd=100.0)

    client.place_limit_buy(_intent("tok", 30.0, 0.55))  # 30 @ 0.50 = 15
    books["tok"] = _book("tok", asks=[(0.60, 100)])
    client.place_limit_buy(_intent("tok", 20.0, 0.65))  # 20 @ 0.60 = 12

    pos = client.positions()[0]
    assert pos.shares == pytest.approx(50.0)
    assert pos.avg_price == pytest.approx((15 + 12) / 50)  # 0.54
    assert client.collateral_balance_usd() == pytest.approx(100 - 15 - 12)  # 73


def test_dry_run_order_ids_are_deterministic() -> None:
    client = DryRunExecutionClient(lambda t: None)
    first = client.place_limit_buy(_intent("a", 1.0, 0.5))
    second = client.place_limit_buy(_intent("b", 1.0, 0.5))
    assert first.order_id == "dry-1"
    assert second.order_id == "dry-2"


def test_dry_run_unknown_order_status_raises() -> None:
    client = DryRunExecutionClient(lambda t: None)
    with pytest.raises(ValueError):
        client.order_status("nope")


# ── normalize_clob_status ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("LIVE", STATE_OPEN),
        ("live", STATE_OPEN),
        ("OPEN", STATE_OPEN),
        ("MATCHED", STATE_FILLED),
        ("DELAYED", STATE_SUBMITTED),
        ("CANCELED", STATE_CANCELED),
        ("CANCELLED", STATE_CANCELED),
        ("REJECTED", STATE_REJECTED),
        ("something-weird", STATE_FAILED),
        ("", STATE_FAILED),
    ],
)
def test_normalize_status_mappings(raw: str, expected: str) -> None:
    assert normalize_clob_status(raw, 0.0, 10.0) == expected


def test_normalize_status_full_fill_overrides_status() -> None:
    # filled >= total (> 0) is FILLED regardless of the reported status.
    assert normalize_clob_status("LIVE", 10.0, 10.0) == STATE_FILLED


def test_normalize_status_partial_cancel_stays_canceled() -> None:
    assert normalize_clob_status("CANCELED", 5.0, 10.0) == STATE_CANCELED


def test_normalize_status_zero_total_uses_string() -> None:
    # No fill shortcut when total is unknown (0); rely on the status string.
    assert normalize_clob_status("LIVE", 0.0, 0.0) == STATE_OPEN


# ── assert_transition guard ──────────────────────────────────────────────────────
def test_assert_transition_allows_legal_moves() -> None:
    assert_transition(STATE_OPEN, STATE_FILLED)
    assert_transition(STATE_SUBMITTED, STATE_OPEN)


def test_assert_transition_rejects_illegal_jump() -> None:
    with pytest.raises(ValueError):
        assert_transition(STATE_FILLED, STATE_OPEN)  # terminal -> anything
    with pytest.raises(ValueError):
        assert_transition(STATE_PENDING, STATE_FILLED)  # PENDING can't reach FILLED


# ── PyClobExecutionClient (no network) ───────────────────────────────────────────
def test_pyclob_raises_without_package_installed() -> None:
    if importlib.util.find_spec("py_clob_client") is not None:
        pytest.skip("py-clob-client is installed; missing-package path not exercised")
    with pytest.raises(RuntimeError):
        PyClobExecutionClient(LiveCredentials(private_key="0xabc"))
