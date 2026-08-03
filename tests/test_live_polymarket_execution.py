"""Tests for the Polymarket SDK adapter (polytempo.live.execution).

Still no network: a fake SDK client returns real ``polymarket`` response
models, so field access is checked against the shipped schemas. Skipped
entirely when the ``live`` extra is not installed.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

pytest.importorskip("polymarket", reason="live extra not installed")

from polymarket import (  # noqa: E402
    AcceptedOrder,
    BalanceAllowance,
    CancelOrdersResponse,
    OpenOrder,
    Position,
    RejectedOrder,
)

from polytempo.live.execution import (  # noqa: E402
    PolymarketExecutionClient,
    normalize_open_order,
)
from polytempo.live.models import (  # noqa: E402
    MODE_LIVE,
    SIDE_YES,
    STATE_FILLED,
    STATE_OPEN,
    STATE_REJECTED,
    OrderIntent,
)

CONDITION_ID = "0x" + "11" * 32


def _intent(shares: float = 50.0, limit_price: float = 0.55) -> OrderIntent:
    return OrderIntent(
        intent_id="i-1",
        event_id="evt",
        bucket_label="26°C",
        token_id="tok",
        market_side=SIDE_YES,
        limit_price=limit_price,
        shares=shares,
        stake_usd=shares * limit_price,
        knob_id="knob",
        mode=MODE_LIVE,
        ts_utc="2026-07-18T00:00:00+00:00",
    )


def _open_order(
    order_id: str = "ord-1",
    *,
    status: str = "LIVE",
    original_size: str = "50",
    size_matched: str = "0",
    price: str = "0.55",
) -> OpenOrder:
    return OpenOrder.model_validate(
        {
            "id": order_id,
            "market": CONDITION_ID,
            "asset_id": "tok",
            "owner": "owner",
            "maker_address": "0x" + "22" * 20,
            "side": "BUY",
            "price": price,
            "original_size": original_size,
            "size_matched": size_matched,
            "outcome": "Yes",
            "order_type": "GTC",
            "status": status,
            "created_at": 1_700_000_000,
        }
    )


def _accepted(status: str, making: str = "0", taking: str = "0", trades=()) -> AcceptedOrder:
    return AcceptedOrder(
        order_id="ord-1",
        status=status,  # type: ignore[arg-type]
        making_amount=Decimal(making),
        taking_amount=Decimal(taking),
        trade_ids=trades,
        transactions_hashes=(),
    )


class _FakePaginator:
    def __init__(self, items) -> None:
        self._items = items

    def iter_items(self):
        return iter(self._items)


class FakeSdkClient:
    """Stands in for ``polymarket.SecureClient``; records the kwargs it gets."""

    def __init__(self, **responses: object) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []

    def _respond(self, name: str, **kwargs: object) -> object:
        self.calls.append((name, kwargs))
        value = self._responses[name]
        if isinstance(value, Exception):
            raise value
        return value

    def place_limit_order(self, **kwargs: object) -> object:
        return self._respond("place_limit_order", **kwargs)

    def get_order(self, **kwargs: object) -> object:
        return self._respond("get_order", **kwargs)

    def cancel_order(self, **kwargs: object) -> object:
        return self._respond("cancel_order", **kwargs)

    def get_balance_allowance(self, **kwargs: object) -> object:
        return self._respond("get_balance_allowance", **kwargs)

    def list_open_orders(self, **kwargs: object) -> _FakePaginator:
        return _FakePaginator(self._respond("list_open_orders", **kwargs))

    def list_positions(self, **kwargs: object) -> _FakePaginator:
        return _FakePaginator(self._respond("list_positions", **kwargs))


def _client(**responses: object) -> tuple[PolymarketExecutionClient, FakeSdkClient]:
    """Wrap a fake SDK client; ``__init__`` would derive API creds over HTTP."""
    sdk = FakeSdkClient(**responses)
    client = object.__new__(PolymarketExecutionClient)
    client._client = sdk
    return client, sdk


# ── place_limit_buy ──────────────────────────────────────────────────────────────
def test_place_limit_buy_live_rests_open() -> None:
    client, sdk = _client(place_limit_order=_accepted("live"))

    placed = client.place_limit_buy(_intent())

    assert placed.order_id == "ord-1"
    assert placed.intent_id == "i-1"
    assert placed.state == STATE_OPEN
    assert placed.raw["filled_shares"] == pytest.approx(0.0)
    assert placed.raw["avg_fill_price"] is None
    assert sdk.calls == [
        ("place_limit_order", {"token_id": "tok", "price": 0.55, "size": 50.0, "side": "BUY"})
    ]


def test_place_limit_buy_matched_derives_fill_from_making_over_taking() -> None:
    # A BUY makes collateral and takes shares: 25 USD spent for 50 shares.
    client, _ = _client(place_limit_order=_accepted("matched", "25.0", "50", ("t1",)))

    placed = client.place_limit_buy(_intent())

    assert placed.state == STATE_FILLED
    assert placed.raw["filled_shares"] == pytest.approx(50.0)
    assert placed.raw["avg_fill_price"] == pytest.approx(0.50)
    assert placed.raw["trade_ids"] == ["t1"]


def test_place_limit_buy_delayed_is_open() -> None:
    # DELAYED is acknowledged with an id, and SUBMITTED -> SUBMITTED is illegal.
    client, _ = _client(place_limit_order=_accepted("delayed"))
    assert client.place_limit_buy(_intent()).state == STATE_OPEN


def test_place_limit_buy_rejection_keeps_code_and_message() -> None:
    rejected = RejectedOrder(code="not_enough_balance", message="not enough balance / allowance")
    client, _ = _client(place_limit_order=rejected)

    placed = client.place_limit_buy(_intent())

    assert placed.state == STATE_REJECTED
    assert placed.order_id == ""
    assert placed.raw == {
        "code": "not_enough_balance",
        "message": "not enough balance / allowance",
    }


def test_place_limit_buy_wraps_sdk_failure() -> None:
    client, _ = _client(place_limit_order=ValueError("boom"))
    with pytest.raises(RuntimeError, match="place_limit_order failed"):
        client.place_limit_buy(_intent())


# ── normalize_open_order ─────────────────────────────────────────────────────────
def test_normalize_open_order_partial_fill() -> None:
    status = normalize_open_order(_open_order(size_matched="20"))

    assert status.order_id == "ord-1"
    assert status.state == STATE_OPEN
    assert status.filled_shares == pytest.approx(20.0)
    assert status.avg_fill_price == pytest.approx(0.55)
    assert status.raw["status"] == "LIVE"


def test_normalize_open_order_unfilled_has_no_avg_price() -> None:
    status = normalize_open_order(_open_order())
    assert status.filled_shares == pytest.approx(0.0)
    assert status.avg_fill_price is None


def test_normalize_open_order_full_match_is_filled() -> None:
    assert normalize_open_order(_open_order(status="MATCHED", size_matched="50")).state == (
        STATE_FILLED
    )


# ── order_status / cancel / open_orders ──────────────────────────────────────────
def test_order_status_reads_get_order() -> None:
    client, sdk = _client(get_order=_open_order("ord-9", size_matched="10"))

    status = client.order_status("ord-9")

    assert status.order_id == "ord-9"
    assert status.filled_shares == pytest.approx(10.0)
    assert sdk.calls == [("get_order", {"order_id": "ord-9"})]


def test_order_status_wraps_sdk_failure() -> None:
    client, _ = _client(get_order=ValueError("boom"))
    with pytest.raises(RuntimeError, match="get_order failed"):
        client.order_status("ord-9")


def test_cancel_order_true_when_id_was_canceled() -> None:
    client, sdk = _client(
        cancel_order=CancelOrdersResponse.model_validate(
            {"canceled": ["ord-1"], "not_canceled": {}}
        )
    )
    assert client.cancel_order("ord-1") is True
    assert sdk.calls == [("cancel_order", {"order_id": "ord-1"})]


def test_cancel_order_false_when_refused() -> None:
    client, _ = _client(
        cancel_order=CancelOrdersResponse.model_validate(
            {"canceled": [], "not_canceled": {"ord-1": "order already matched"}}
        )
    )
    assert client.cancel_order("ord-1") is False


def test_open_orders_drops_terminal_entries() -> None:
    client, _ = _client(
        list_open_orders=[
            _open_order("ord-1", size_matched="10"),
            _open_order("ord-2", status="CANCELED"),
        ]
    )

    orders = client.open_orders()

    assert [o.order_id for o in orders] == ["ord-1"]
    assert orders[0].state == STATE_OPEN


def test_open_orders_wraps_sdk_failure() -> None:
    client, _ = _client(list_open_orders=ValueError("boom"))
    with pytest.raises(RuntimeError, match="list_open_orders failed"):
        client.open_orders()


# ── positions / balance ──────────────────────────────────────────────────────────
def test_positions_map_size_and_avg_price() -> None:
    client, sdk = _client(
        list_positions=[
            Position.model_validate(
                {"conditionId": CONDITION_ID, "asset": "tok", "size": "12.5", "avgPrice": "0.4"}
            ),
            Position.model_validate({"conditionId": CONDITION_ID, "size": "3"}),  # no token id
        ]
    )

    positions = client.positions()

    assert len(positions) == 1
    assert positions[0].token_id == "tok"
    assert positions[0].shares == pytest.approx(12.5)
    assert positions[0].avg_price == pytest.approx(0.4)
    # No explicit user: the SDK defaults to the authenticated wallet.
    assert sdk.calls == [("list_positions", {})]


def test_collateral_balance_scales_six_decimals() -> None:
    client, sdk = _client(
        get_balance_allowance=BalanceAllowance.model_validate(
            {"balance": "1234560", "allowances": {}}
        )
    )

    assert client.collateral_balance_usd() == pytest.approx(1.23456)
    assert sdk.calls == [("get_balance_allowance", {"asset_type": "COLLATERAL"})]


def test_collateral_balance_is_none_on_failure() -> None:
    client, _ = _client(get_balance_allowance=ValueError("boom"))
    assert client.collateral_balance_usd() is None
