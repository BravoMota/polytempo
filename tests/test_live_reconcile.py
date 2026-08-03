"""Tests for reconciliation (fake ledger + fake client, no DB, no network)."""

from __future__ import annotations

from dataclasses import dataclass, field

from polytempo.live.models import (
    STATE_FAILED,
    STATE_REJECTED,
    LivePosition,
    OrderStatus,
)
from polytempo.live.reconcile import reconcile

NOW = "2026-07-18T00:00:00+00:00"


@dataclass
class FakeLedger:
    unresolved: list[dict] = field(default_factory=list)
    positions: dict[str, float] = field(default_factory=dict)
    resolved: list[tuple[str, str, str | None]] = field(default_factory=list)

    def unresolved_intents(self) -> list[dict]:
        return self.unresolved

    def open_positions_by_token(self) -> dict[str, float]:
        return self.positions

    def resolve_stale_intent(
        self, intent_id: str, state: str, *, message: str | None = None
    ) -> None:
        self.resolved.append((intent_id, state, message))


@dataclass
class FakeClient:
    open: list[OrderStatus] = field(default_factory=list)
    statuses: dict[str, OrderStatus] = field(default_factory=dict)
    exchange_positions: list[LivePosition] = field(default_factory=list)

    def open_orders(self) -> list[OrderStatus]:
        return self.open

    def order_status(self, order_id: str) -> OrderStatus:
        return self.statuses[order_id]

    def positions(self) -> list[LivePosition]:
        return self.exchange_positions


def _status(order_id: str, state: str, filled: float = 0.0) -> OrderStatus:
    return OrderStatus(
        order_id=order_id,
        state=state,
        filled_shares=filled,
        avg_fill_price=0.3 if filled else None,
    )


def test_still_open_is_informational_only() -> None:
    ledger = FakeLedger(
        unresolved=[{"intent_id": "i1", "order_id": "o1", "token_id": "t1"}]
    )
    client = FakeClient(open=[_status("o1", "OPEN")])

    report = reconcile(ledger, client, now_iso=NOW)

    assert report.ok is True
    assert report.discrepancies == ["intent i1 still open on exchange"]
    assert report.resolved_intents == []
    assert ledger.resolved == []


def test_resolvable_no_fills_writes_terminal() -> None:
    ledger = FakeLedger(
        unresolved=[{"intent_id": "i2", "order_id": "o2", "token_id": "t2"}]
    )
    client = FakeClient(statuses={"o2": _status("o2", STATE_REJECTED)})

    report = reconcile(ledger, client, now_iso=NOW)

    assert report.ok is True
    assert report.discrepancies == []
    assert report.resolved_intents == ["i2"]
    assert ledger.resolved == [("i2", STATE_REJECTED, "reconciled")]


def test_fills_need_manual_review_and_not_resolved() -> None:
    ledger = FakeLedger(
        unresolved=[{"intent_id": "i3", "order_id": "o3", "token_id": "t3"}]
    )
    client = FakeClient(statuses={"o3": _status("o3", STATE_FAILED, filled=5.0)})

    report = reconcile(ledger, client, now_iso=NOW)

    assert report.ok is False
    assert report.discrepancies == [
        "intent i3 had fills on exchange; manual review"
    ]
    assert report.resolved_intents == []
    assert ledger.resolved == []


def test_no_ack_resolves_failed_but_flags_for_verification() -> None:
    ledger = FakeLedger(
        unresolved=[{"intent_id": "i4", "order_id": None, "token_id": "t4"}]
    )
    client = FakeClient()

    report = reconcile(ledger, client, now_iso=NOW)

    assert report.ok is False
    assert report.resolved_intents == ["i4"]
    assert ledger.resolved == [
        ("i4", STATE_FAILED, "no ack; assumed never placed")
    ]
    assert report.discrepancies == [
        "intent i4 had no ack; verify no order exists on exchange"
    ]


def test_position_mismatch_flagged() -> None:
    ledger = FakeLedger(positions={"tokA": 10.0})
    client = FakeClient(exchange_positions=[LivePosition("tokA", 7.0)])

    report = reconcile(ledger, client, now_iso=NOW)

    assert report.ok is False
    assert len(report.discrepancies) == 1
    assert "position mismatch on tokA" in report.discrepancies[0]


def test_positions_match_exactly_is_ok() -> None:
    ledger = FakeLedger(positions={"tokA": 10.0})
    client = FakeClient(exchange_positions=[LivePosition("tokA", 10.0)])

    report = reconcile(ledger, client, now_iso=NOW)

    assert report.ok is True
    assert report.discrepancies == []


def test_foreign_exchange_position_is_ignored() -> None:
    # The wallet is shared with manual trading: a token the node never traded
    # must not block startup (it used to, via a union over both sides).
    ledger = FakeLedger(positions={})
    client = FakeClient(exchange_positions=[LivePosition("tokManual", 2.8169)])

    report = reconcile(ledger, client, now_iso=NOW)

    assert report.ok is True
    assert report.discrepancies == []


def test_drift_on_a_traded_token_is_still_flagged_when_exchange_is_empty() -> None:
    ledger = FakeLedger(positions={"tokA": 10.0})
    client = FakeClient(exchange_positions=[LivePosition("tokManual", 5.0)])

    report = reconcile(ledger, client, now_iso=NOW)

    assert report.ok is False
    assert "position mismatch on tokA" in report.discrepancies[0]
    assert "tokManual" not in " ".join(report.discrepancies)
