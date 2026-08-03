"""Startup/periodic reconciliation between the ledger and the exchange.

This is a report generator: it resolves crash-orphaned intents in the ledger
(by writing the missing terminal RESULT row) and flags any position drift, but
it NEVER places or cancels orders. Anything it cannot resolve safely is left as
a discrepancy for an operator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from polytempo.live.models import STATE_FAILED

if TYPE_CHECKING:
    from polytempo.live.execution import ExecutionClient
    from polytempo.live.ledger import LiveLedger


@dataclass(frozen=True)
class ReconcileReport:
    ok: bool
    discrepancies: list[str]
    resolved_intents: list[str]


def reconcile(
    ledger: "LiveLedger", client: "ExecutionClient", *, now_iso: str
) -> ReconcileReport:
    """Reconcile unresolved intents and positions against the exchange."""
    discrepancies: list[str] = []
    informational: set[str] = set()
    resolved: list[str] = []

    open_order_ids = {_order_id_of(o) for o in client.open_orders()}

    # a) resolve crash-orphaned intents (INTENT written, no RESULT yet).
    for item in ledger.unresolved_intents():
        intent_id = item["intent_id"]
        order_id = item.get("order_id")

        if order_id is not None and order_id in open_order_ids:
            line = f"intent {intent_id} still open on exchange"
            discrepancies.append(line)
            informational.add(line)
            continue

        if order_id is not None:
            status = client.order_status(order_id)
            if status.filled_shares and status.filled_shares > 0:
                discrepancies.append(
                    f"intent {intent_id} had fills on exchange; manual review"
                )
                continue
            ledger.resolve_stale_intent(intent_id, status.state, message="reconciled")
            resolved.append(intent_id)
        else:
            ledger.resolve_stale_intent(
                intent_id, STATE_FAILED, message="no ack; assumed never placed"
            )
            resolved.append(intent_id)
            discrepancies.append(
                f"intent {intent_id} had no ack; verify no order exists on exchange"
            )

    # b) position drift: ledger filled-unsettled shares vs exchange positions.
    # Only tokens the ledger has a position in. The wallet is shared with manual
    # trading, and a token the node never touched is not drift — comparing the
    # union would let any hand-placed bet block startup in live mode.
    ledger_positions = ledger.open_positions_by_token()
    exchange_positions = {p.token_id: p.shares for p in client.positions()}
    for token in sorted(ledger_positions):
        ledger_shares = ledger_positions[token]
        exchange_shares = exchange_positions.get(token, 0.0)
        if abs(ledger_shares - exchange_shares) > 0.01:
            discrepancies.append(
                f"position mismatch on {token}: "
                f"ledger {ledger_shares} vs exchange {exchange_shares}"
            )

    ok = not [d for d in discrepancies if d not in informational]
    return ReconcileReport(ok=ok, discrepancies=discrepancies, resolved_intents=resolved)


def _order_id_of(order: object) -> str:
    """Extract an order id whether ``open_orders`` yields objects or bare ids."""
    return getattr(order, "order_id", order)  # type: ignore[return-value]
