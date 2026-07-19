"""Place-and-manage loop: drive one ``OrderIntent`` to a terminal outcome.

``manage_order`` places a limit buy, polls its status until it settles or the
timeout elapses, and on timeout cancels and trusts the final status (a cancel
can race a fill). The clock and sleep are injectable so tests never block.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from polytempo.live.execution import ExecutionClient
from polytempo.live.models import (
    STATE_CANCELED,
    STATE_FAILED,
    TERMINAL_STATES,
    ExecutionResult,
    OrderIntent,
    OrderStatus,
)


def manage_order(
    client: ExecutionClient,
    intent: OrderIntent,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float = 2.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.monotonic,
    on_transition: Callable[[str, OrderStatus | None], None] | None = None,
) -> ExecutionResult:
    """Place ``intent`` and manage it to a terminal ``ExecutionResult``."""
    try:
        placed = client.place_limit_buy(intent)
    except Exception as exc:  # noqa: BLE001 — any placement error is a FAILED result
        return ExecutionResult(
            intent=intent,
            state=STATE_FAILED,
            order_id=None,
            filled_shares=0.0,
            avg_fill_price=None,
            message=str(exc),
        )

    order_id = placed.order_id
    last_state: str | None = None

    def emit(state: str, status: OrderStatus | None) -> None:
        nonlocal last_state
        if state != last_state:
            last_state = state
            if on_transition is not None:
                on_transition(state, status)

    # Journal the ack with its order id immediately: if we crash before the
    # first poll, reconciliation must know the order was placed.
    ack = OrderStatus(
        order_id=order_id,
        state=placed.state,
        filled_shares=0.0,
        avg_fill_price=None,
        raw=placed.raw,
    )
    emit(placed.state, ack)

    # A terminal placement with no order id (e.g. REJECTED before entry) has
    # nothing to poll. With an order id, fall through so the first status poll
    # reports the true fill numbers.
    if placed.state in TERMINAL_STATES and not order_id:
        return _result(intent, order_id, ack)

    deadline = now_fn() + timeout_seconds
    while True:
        try:
            status = client.order_status(order_id)
        except Exception as exc:  # noqa: BLE001 — state unknown; reconcile resolves it
            return ExecutionResult(
                intent=intent,
                state=STATE_FAILED,
                order_id=order_id,
                filled_shares=0.0,
                avg_fill_price=None,
                message=f"order_status failed: {exc}",
            )
        emit(status.state, status)
        if status.state in TERMINAL_STATES:
            return _result(intent, order_id, status)
        if now_fn() >= deadline:
            break
        sleep_fn(poll_interval_seconds)

    # Timed out while still resting: cancel, then trust whatever the exchange
    # reports (the cancel may have raced a fill).
    try:
        client.cancel_order(order_id)
    except Exception:  # noqa: BLE001 — a failed cancel doesn't change the truth on-book
        pass
    try:
        final = client.order_status(order_id)
    except Exception as exc:  # noqa: BLE001 — state unknown; reconcile resolves it
        return ExecutionResult(
            intent=intent,
            state=STATE_FAILED,
            order_id=order_id,
            filled_shares=0.0,
            avg_fill_price=None,
            message=f"order_status after cancel failed: {exc}",
        )
    emit(final.state, final)
    if final.state in TERMINAL_STATES:
        return _result(intent, order_id, final)
    # Cancel accepted but the status read still shows it resting: report CANCELED,
    # keeping any partial fill we observed.
    return _result(intent, order_id, final, forced_state=STATE_CANCELED)


def _result(
    intent: OrderIntent,
    order_id: str,
    status: OrderStatus,
    forced_state: str | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        intent=intent,
        state=forced_state or status.state,
        order_id=order_id,
        filled_shares=status.filled_shares,
        avg_fill_price=status.avg_fill_price,
    )
