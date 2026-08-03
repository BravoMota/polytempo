"""Execution clients for the live trading node.

Three pieces:

* ``ExecutionClient`` — the structural protocol the node depends on.
* ``DryRunExecutionClient`` — a network-free simulator that matches intents
  against a supplied book, so the whole stack can run end-to-end offline.
* ``PolymarketExecutionClient`` — the real adapter over ``polymarket-client``.

``polymarket-client`` is imported lazily and *only* inside
``PolymarketExecutionClient`` so this module (and ``normalize_clob_status``)
import cleanly without the extra installed. All exchange-status normalization
lives in the module-level pure function ``normalize_clob_status`` so it is
unit-testable on its own.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from polytempo.live.config import LiveCredentials
from polytempo.live.models import (
    STATE_CANCELED,
    STATE_FAILED,
    STATE_FILLED,
    STATE_OPEN,
    STATE_REJECTED,
    STATE_SUBMITTED,
    TERMINAL_STATES,
    BookDepth,
    LivePosition,
    OrderIntent,
    OrderStatus,
    PlacedOrder,
    assert_transition,
)

if TYPE_CHECKING:
    from polymarket import OpenOrder

_EPS = 1e-9
_MISSING_EXTRA = 'install extra: pip install "polytempo[live]"'


class ExecutionClient(Protocol):
    """What the node needs from any execution backend."""

    def place_limit_buy(self, intent: OrderIntent) -> PlacedOrder: ...

    def order_status(self, order_id: str) -> OrderStatus: ...

    def cancel_order(self, order_id: str) -> bool: ...

    def open_orders(self) -> list[OrderStatus]: ...

    def positions(self) -> list[LivePosition]: ...

    def collateral_balance_usd(self) -> float | None: ...


# ── Dry-run simulator ───────────────────────────────────────────────────────────
class DryRunExecutionClient:
    """In-memory execution client: no network, deterministic order ids.

    ``book_provider(token_id)`` returns the current book for a token (or
    ``None`` if unavailable). Orders match immediately against that book's asks
    at prices ``<= limit_price``; the unfilled remainder rests ``OPEN``.
    """

    def __init__(
        self,
        book_provider: Callable[[str], BookDepth | None],
        starting_balance_usd: float = 0.0,
    ) -> None:
        self._book_provider = book_provider
        self._balance = starting_balance_usd
        self._orders: dict[str, OrderStatus] = {}
        self._positions: dict[str, LivePosition] = {}
        self._next_id = 1

    def place_limit_buy(self, intent: OrderIntent) -> PlacedOrder:
        book = self._book_provider(intent.token_id)
        filled, cost = _simulate_fill(book, intent.limit_price, intent.shares)

        order_id = f"dry-{self._next_id}"
        self._next_id += 1

        final_state = STATE_FILLED if filled + _EPS >= intent.shares else STATE_OPEN
        assert_transition(STATE_SUBMITTED, final_state)

        avg_price = cost / filled if filled > 0 else None
        self._orders[order_id] = OrderStatus(
            order_id=order_id,
            state=final_state,
            filled_shares=filled,
            avg_fill_price=avg_price,
        )
        if filled > 0:
            self._apply_fill(intent.token_id, filled, cost)

        return PlacedOrder(
            order_id=order_id,
            intent_id=intent.intent_id,
            state=final_state,
        )

    def order_status(self, order_id: str) -> OrderStatus:
        status = self._orders.get(order_id)
        if status is None:
            raise ValueError(f"unknown order: {order_id}")
        return status

    def cancel_order(self, order_id: str) -> bool:
        status = self._orders.get(order_id)
        if status is None or status.state != STATE_OPEN:
            return False
        assert_transition(status.state, STATE_CANCELED)
        self._orders[order_id] = OrderStatus(
            order_id=order_id,
            state=STATE_CANCELED,
            filled_shares=status.filled_shares,
            avg_fill_price=status.avg_fill_price,
        )
        return True

    def open_orders(self) -> list[OrderStatus]:
        return [s for s in self._orders.values() if s.state == STATE_OPEN]

    def positions(self) -> list[LivePosition]:
        return [p for p in self._positions.values() if p.shares != 0]

    def collateral_balance_usd(self) -> float | None:
        return self._balance

    def _apply_fill(self, token_id: str, shares: float, cost: float) -> None:
        self._balance -= cost
        existing = self._positions.get(token_id)
        if existing is None:
            self._positions[token_id] = LivePosition(
                token_id=token_id, shares=shares, avg_price=cost / shares
            )
            return
        total_shares = existing.shares + shares
        total_cost = (existing.avg_price or 0.0) * existing.shares + cost
        self._positions[token_id] = LivePosition(
            token_id=token_id,
            shares=total_shares,
            avg_price=total_cost / total_shares,
        )


def _simulate_fill(
    book: BookDepth | None,
    limit_price: float,
    shares_wanted: float,
) -> tuple[float, float]:
    """Return (filled_shares, cost) matching ``shares_wanted`` against asks."""
    if book is None:
        return 0.0, 0.0
    filled = 0.0
    cost = 0.0
    for level in book.asks:
        if level.price > limit_price or filled + _EPS >= shares_wanted:
            break
        take = min(level.size, shares_wanted - filled)
        filled += take
        cost += take * level.price
    return filled, cost


# ── Real CLOB adapter ───────────────────────────────────────────────────────────
def normalize_clob_status(raw_status: str, filled: float, total: float) -> str:
    """Map a raw CLOB order status to our ``STATE_*`` vocabulary (pure).

    A fully matched order (``filled >= total`` with ``total > 0``) is ``FILLED``
    regardless of the reported status. Otherwise the status string is mapped;
    anything unrecognized becomes ``FAILED`` (state unknown, needs reconcile).

    ``DELAYED`` is ``OPEN``, not ``SUBMITTED``: the exchange has acknowledged
    the order and returned an id, it just has not matched yet. That also keeps
    every mapped state reachable from ``SUBMITTED`` per ``ORDER_TRANSITIONS``.
    """
    if total > 0 and filled + _EPS >= total:
        return STATE_FILLED
    status = (raw_status or "").strip().upper()
    mapping = {
        "LIVE": STATE_OPEN,
        "DELAYED": STATE_OPEN,
        "MATCHED": STATE_FILLED,
        "UNMATCHED": STATE_REJECTED,
        "CANCELED": STATE_CANCELED,
    }
    return mapping.get(status, STATE_FAILED)


def normalize_open_order(order: "OpenOrder") -> OrderStatus:
    """Normalize one SDK ``OpenOrder`` into our ``OrderStatus`` (pure).

    Fills come from the order's own ``size_matched``/``price`` rather than the
    placement response's making/taking amounts: those only cover fills that
    happened at placement, while a resting order keeps filling afterwards.
    ``price`` is the limit price, so it is an upper bound on a BUY's true
    average — the exchange may improve it.
    """
    filled = float(order.size_matched)
    total = float(order.original_size)
    return OrderStatus(
        order_id=str(order.id),
        state=normalize_clob_status(order.status, filled, total),
        filled_shares=filled,
        avg_fill_price=float(order.price) if filled > 0 else None,
        raw=order.model_dump(mode="json"),
    )


class PolymarketExecutionClient:
    """Execution against Polymarket CLOB V2 via the ``polymarket-client`` SDK.

    The SDK derives the wallet type and the pUSD/exchange addresses from its
    production ``Environment``, so credentials are just a signing key plus the
    wallet to act for. Every call is wrapped in ``RuntimeError`` with context so
    the place-and-manage loop journals a FAILED result instead of crashing, and
    the SDK import stays inside this class.
    """

    def __init__(self, credentials: LiveCredentials) -> None:
        try:
            from polymarket import SecureClient
        except ImportError as exc:
            raise RuntimeError(_MISSING_EXTRA) from exc
        try:
            self._client = SecureClient.create(
                private_key=credentials.private_key,
                wallet=credentials.wallet_address,
            )
        except Exception as exc:  # noqa: BLE001 — surface any setup failure with context
            raise RuntimeError(f"failed to initialize Polymarket client: {exc}") from exc

    def place_limit_buy(self, intent: OrderIntent) -> PlacedOrder:
        try:
            response = self._client.place_limit_order(
                token_id=intent.token_id,
                price=intent.limit_price,
                size=intent.shares,
                side="BUY",
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"place_limit_order failed: {exc}") from exc

        if not response.ok:
            # Carry the machine-readable code/message so the ledger can journal
            # why the exchange refused (``not_enough_balance`` and friends).
            state = STATE_REJECTED
            order_id = ""
            raw: dict[str, object] = {"code": response.code, "message": response.message}
        else:
            state = normalize_clob_status(response.status, 0.0, intent.shares)
            order_id = response.order_id
            # A BUY makes collateral and takes shares, so ``making_amount`` is
            # USD spent and ``taking_amount`` is shares received at placement.
            shares = float(response.taking_amount)
            raw = {
                "status": response.status,
                "filled_shares": shares,
                "avg_fill_price": float(response.making_amount) / shares if shares else None,
                "trade_ids": list(response.trade_ids),
            }
        assert_transition(STATE_SUBMITTED, state)
        return PlacedOrder(
            order_id=order_id,
            intent_id=intent.intent_id,
            state=state,
            raw=raw,
        )

    def order_status(self, order_id: str) -> OrderStatus:
        try:
            order = self._client.get_order(order_id=order_id)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"get_order failed: {exc}") from exc
        return normalize_open_order(order)

    def cancel_order(self, order_id: str) -> bool:
        try:
            response = self._client.cancel_order(order_id=order_id)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"cancel_order failed: {exc}") from exc
        return str(order_id) in [str(c) for c in response.canceled]

    def open_orders(self) -> list[OrderStatus]:
        try:
            orders = list(self._client.list_open_orders().iter_items())
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"list_open_orders failed: {exc}") from exc
        statuses = [normalize_open_order(o) for o in orders]
        return [s for s in statuses if s.state not in TERMINAL_STATES]

    def positions(self) -> list[LivePosition]:
        # No ``user``: the SDK defaults to the authenticated wallet.
        try:
            items = list(self._client.list_positions().iter_items())
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"list_positions failed: {exc}") from exc
        return [
            LivePosition(
                token_id=str(p.token_id),
                shares=float(p.size) if p.size is not None else 0.0,
                avg_price=float(p.avg_price) if p.avg_price is not None else None,
            )
            for p in items
            if p.token_id is not None
        ]

    def collateral_balance_usd(self) -> float | None:
        try:
            allowance = self._client.get_balance_allowance(asset_type="COLLATERAL")
        except Exception:  # noqa: BLE001 — best-effort; balance is optional
            return None
        # pUSD collateral is reported in 6-decimal base units.
        return allowance.balance / 1e6
