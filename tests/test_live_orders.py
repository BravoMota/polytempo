"""Tests for the place-and-manage loop (polytempo.live.orders).

Fully synchronous: the clock and sleep are injected so nothing blocks, and a
small in-memory fake client scripts the status sequence.
"""

from __future__ import annotations

import pytest

from polytempo.live.models import (
    MODE_DRY_RUN,
    SIDE_YES,
    STATE_CANCELED,
    STATE_FAILED,
    STATE_FILLED,
    STATE_OPEN,
    STATE_SUBMITTED,
    OrderIntent,
    OrderStatus,
    PlacedOrder,
)
from polytempo.live.orders import manage_order


def _intent() -> OrderIntent:
    return OrderIntent(
        intent_id="i-1",
        event_id="evt",
        bucket_label="26°C",
        token_id="tok",
        market_side=SIDE_YES,
        limit_price=0.55,
        shares=50.0,
        stake_usd=27.5,
        knob_id="knob",
        mode=MODE_DRY_RUN,
        ts_utc="2026-07-18T00:00:00+00:00",
    )


class FakeClient:
    """Scripts placement + a fixed sequence of status reads."""

    def __init__(
        self,
        *,
        statuses,
        place_state=STATE_OPEN,
        place_raises=None,
        cancel_result=True,
        order_id="ord-1",
    ) -> None:
        self._statuses = list(statuses)
        self._i = 0
        self._place_state = place_state
        self._place_raises = place_raises
        self._cancel_result = cancel_result
        self._order_id = order_id
        self.cancel_calls = 0

    def place_limit_buy(self, intent):
        if self._place_raises is not None:
            raise self._place_raises
        return PlacedOrder(order_id=self._order_id, intent_id=intent.intent_id, state=self._place_state)

    def order_status(self, order_id):
        status = self._statuses[min(self._i, len(self._statuses) - 1)]
        self._i += 1
        return status

    def cancel_order(self, order_id):
        self.cancel_calls += 1
        return self._cancel_result

    def open_orders(self):
        return []

    def positions(self):
        return []

    def collateral_balance_usd(self):
        return None


def _clock(values):
    """A ``now_fn`` that returns successive values, repeating the last."""
    seq = list(values)
    idx = {"i": 0}

    def now() -> float:
        i = min(idx["i"], len(seq) - 1)
        idx["i"] += 1
        return seq[i]

    return now


def test_happy_path_fills() -> None:
    client = FakeClient(
        place_state=STATE_SUBMITTED,
        statuses=[
            OrderStatus("ord-1", STATE_OPEN, 0.0, None),
            OrderStatus("ord-1", STATE_FILLED, 50.0, 0.50),
        ],
    )
    transitions: list[str] = []
    sleeps: list[float] = []

    result = manage_order(
        client,
        _intent(),
        timeout_seconds=100.0,
        poll_interval_seconds=1.0,
        sleep_fn=sleeps.append,
        now_fn=_clock([0.0, 1.0, 2.0]),
        on_transition=lambda state, status: transitions.append(state),
    )

    assert result.state == STATE_FILLED
    assert result.order_id == "ord-1"
    assert result.filled_shares == pytest.approx(50.0)
    assert result.avg_fill_price == pytest.approx(0.50)
    assert transitions == [STATE_SUBMITTED, STATE_OPEN, STATE_FILLED]
    assert sleeps == [1.0]  # one poll gap; injected sleep, never real
    assert client.cancel_calls == 0


def test_timeout_cancels_and_reports_canceled() -> None:
    client = FakeClient(
        place_state=STATE_OPEN,
        statuses=[
            OrderStatus("ord-1", STATE_OPEN, 0.0, None),
            OrderStatus("ord-1", STATE_CANCELED, 0.0, None),
        ],
    )
    transitions: list[str] = []

    result = manage_order(
        client,
        _intent(),
        timeout_seconds=10.0,
        now_fn=_clock([0.0, 50.0]),  # deadline=10; loop check 50 -> timeout
        sleep_fn=lambda _s: None,
        on_transition=lambda state, status: transitions.append(state),
    )

    assert result.state == STATE_CANCELED
    assert client.cancel_calls == 1
    assert transitions == [STATE_OPEN, STATE_CANCELED]


def test_cancel_races_fill_trusts_final_status() -> None:
    client = FakeClient(
        place_state=STATE_OPEN,
        cancel_result=False,  # exchange refuses cancel: it already filled
        statuses=[
            OrderStatus("ord-1", STATE_OPEN, 0.0, None),
            OrderStatus("ord-1", STATE_FILLED, 50.0, 0.50),
        ],
    )

    result = manage_order(
        client,
        _intent(),
        timeout_seconds=10.0,
        now_fn=_clock([0.0, 50.0]),
        sleep_fn=lambda _s: None,
    )

    assert result.state == STATE_FILLED
    assert result.filled_shares == pytest.approx(50.0)
    assert client.cancel_calls == 1


def test_cancel_but_status_still_open_forces_canceled() -> None:
    client = FakeClient(
        place_state=STATE_OPEN,
        statuses=[
            OrderStatus("ord-1", STATE_OPEN, 10.0, 0.50),
            OrderStatus("ord-1", STATE_OPEN, 10.0, 0.50),  # cancel not yet reflected
        ],
    )

    result = manage_order(
        client,
        _intent(),
        timeout_seconds=10.0,
        now_fn=_clock([0.0, 50.0]),
        sleep_fn=lambda _s: None,
    )

    assert result.state == STATE_CANCELED
    assert result.filled_shares == pytest.approx(10.0)  # partial fill kept


def test_place_failure_returns_failed() -> None:
    client = FakeClient(place_raises=RuntimeError("boom"), statuses=[])
    transitions: list[str] = []

    result = manage_order(
        client,
        _intent(),
        timeout_seconds=10.0,
        now_fn=_clock([0.0]),
        sleep_fn=lambda _s: None,
        on_transition=lambda state, status: transitions.append(state),
    )

    assert result.state == STATE_FAILED
    assert result.order_id is None
    assert result.message == "boom"
    assert transitions == []
