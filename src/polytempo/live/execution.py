"""Execution clients for the live trading node.

Three pieces:

* ``ExecutionClient`` — the structural protocol the node depends on.
* ``DryRunExecutionClient`` — a network-free simulator that matches intents
  against a supplied book, so the whole stack can run end-to-end offline.
* ``PyClobExecutionClient`` — the real adapter over ``py-clob-client``.

``py-clob-client`` is imported lazily and *only* inside ``PyClobExecutionClient``
so this module (and ``normalize_clob_status``) import cleanly without the extra
installed. All exchange-status normalization lives in the module-level pure
function ``normalize_clob_status`` so it is unit-testable on its own.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import httpx

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
    """
    if total > 0 and filled + _EPS >= total:
        return STATE_FILLED
    status = (raw_status or "").strip().upper()
    mapping = {
        "LIVE": STATE_OPEN,
        "OPEN": STATE_OPEN,
        "MATCHED": STATE_FILLED,
        "FILLED": STATE_FILLED,
        "DELAYED": STATE_SUBMITTED,
        "CANCELED": STATE_CANCELED,
        "CANCELLED": STATE_CANCELED,
        "REJECTED": STATE_REJECTED,
    }
    return mapping.get(status, STATE_FAILED)


class PyClobExecutionClient:
    """Execution against the real Polymarket CLOB via ``py-clob-client``.

    Written defensively: the ``py-clob-client`` surface has drifted across
    releases, so every touchpoint is guarded and wrapped in ``RuntimeError``
    with context, and all imports of the package stay inside this class.
    """

    _DATA_API = "https://data-api.polymarket.com"

    def __init__(
        self,
        credentials: LiveCredentials,
        host: str = "https://clob.polymarket.com",
        chain_id: int = 137,
    ) -> None:
        try:
            from py_clob_client.client import ClobClient
        except ImportError as exc:
            raise RuntimeError(_MISSING_EXTRA) from exc

        self._credentials = credentials
        self._host = host

        kwargs: dict[str, object] = {"key": credentials.private_key, "chain_id": chain_id}
        if credentials.signature_type is not None:
            kwargs["signature_type"] = credentials.signature_type
        if credentials.proxy_address is not None:
            kwargs["funder"] = credentials.proxy_address

        try:
            client = ClobClient(host, **kwargs)
            client.set_api_creds(client.create_or_derive_api_creds())
        except Exception as exc:  # noqa: BLE001 — surface any setup failure with context
            raise RuntimeError(f"failed to initialize CLOB client: {exc}") from exc
        self._client = client

    def place_limit_buy(self, intent: OrderIntent) -> PlacedOrder:
        try:
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import BUY
        except ImportError as exc:
            raise RuntimeError(_MISSING_EXTRA) from exc

        poster = self._require("create_and_post_order")
        args = OrderArgs(
            price=intent.limit_price,
            size=intent.shares,
            side=BUY,
            token_id=intent.token_id,
        )
        try:
            response = poster(args)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"create_and_post_order failed: {exc}") from exc

        raw = response if isinstance(response, dict) else {}
        order_id = raw.get("orderID") or raw.get("orderId") or raw.get("id")
        raw_status = str(raw.get("status") or "")
        if order_id is None:
            state = STATE_REJECTED
        elif raw_status:
            state = normalize_clob_status(raw_status, 0.0, intent.shares)
        else:
            state = STATE_OPEN
        return PlacedOrder(
            order_id=str(order_id) if order_id is not None else "",
            intent_id=intent.intent_id,
            state=state,
            raw=raw,
        )

    def order_status(self, order_id: str) -> OrderStatus:
        getter = self._require("get_order")
        try:
            raw = getter(order_id)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"get_order failed: {exc}") from exc
        data = raw if isinstance(raw, dict) else {}
        filled = _to_float(data.get("size_matched") or data.get("sizeMatched"))
        total = _to_float(
            data.get("original_size") or data.get("originalSize") or data.get("size")
        )
        state = normalize_clob_status(str(data.get("status") or ""), filled, total)
        avg = _to_optional_float(data.get("price")) if filled > 0 else None
        return OrderStatus(
            order_id=str(order_id),
            state=state,
            filled_shares=filled,
            avg_fill_price=avg,
            raw=data,
        )

    def cancel_order(self, order_id: str) -> bool:
        canceller = self._require("cancel")
        try:
            raw = canceller(order_id)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"cancel failed: {exc}") from exc
        if isinstance(raw, dict):
            canceled = raw.get("canceled") or raw.get("cancelled")
            if isinstance(canceled, list):
                return str(order_id) in [str(c) for c in canceled]
            return bool(raw.get("success", True))
        return bool(raw)

    def open_orders(self) -> list[OrderStatus]:
        getter = self._require("get_orders")
        try:
            raw = getter()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"get_orders failed: {exc}") from exc
        if isinstance(raw, dict):
            items = raw.get("data") or []
        elif isinstance(raw, list):
            items = raw
        else:
            items = []
        result: list[OrderStatus] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            order_id = item.get("id") or item.get("orderID") or item.get("orderId")
            filled = _to_float(item.get("size_matched") or item.get("sizeMatched"))
            total = _to_float(item.get("original_size") or item.get("size"))
            state = normalize_clob_status(str(item.get("status") or ""), filled, total)
            if state in TERMINAL_STATES:
                continue
            result.append(
                OrderStatus(
                    order_id=str(order_id) if order_id is not None else "",
                    state=state,
                    filled_shares=filled,
                    avg_fill_price=_to_optional_float(item.get("price")) if filled > 0 else None,
                    raw=item,
                )
            )
        return result

    def positions(self) -> list[LivePosition]:
        user = self._credentials.proxy_address or self._derived_address()
        if not user:
            return []
        try:
            response = httpx.get(f"{self._DATA_API}/positions", params={"user": user})
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"positions fetch failed: {exc}") from exc
        positions: list[LivePosition] = []
        for item in payload or []:
            if not isinstance(item, dict):
                continue
            asset = item.get("asset")
            if asset is None:
                continue
            positions.append(
                LivePosition(
                    token_id=str(asset),
                    shares=_to_float(item.get("size")),
                    avg_price=_to_optional_float(item.get("avgPrice")),
                )
            )
        return positions

    def collateral_balance_usd(self) -> float | None:
        getter = getattr(self._client, "get_balance_allowance", None)
        if getter is None:
            return None
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

            raw = getter(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        except Exception:  # noqa: BLE001 — best-effort; balance is optional
            return None
        if not isinstance(raw, dict):
            return None
        balance = raw.get("balance")
        if balance is None:
            return None
        try:
            # USDC collateral is reported in 6-decimal base units.
            return float(balance) / 1e6
        except (TypeError, ValueError):
            return None

    def _require(self, name: str) -> Callable[..., object]:
        method = getattr(self._client, name, None)
        if method is None or not callable(method):
            raise RuntimeError(f"py-clob-client ClobClient has no callable {name!r}")
        return method

    def _derived_address(self) -> str | None:
        getter = getattr(self._client, "get_address", None)
        if getter is None:
            return None
        try:
            return getter()
        except Exception:  # noqa: BLE001
            return None


def _to_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _to_optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
