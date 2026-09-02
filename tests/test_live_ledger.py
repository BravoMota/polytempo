"""Full LiveLedger journal flow + NaN-metadata regression (Postgres-backed).

Gated on POLYTEMPO_LIVE_TEST_DATABASE_URL; skipped when unset. The target DB is
disposable — the fixture truncates it before every test.
"""

from __future__ import annotations

import math
import os

import pytest

from polytempo.live.ledger import LiveLedger
from polytempo.live.models import (
    MODE_DRY_RUN,
    SIDE_NO,
    SIDE_YES,
    STATE_CANCELED,
    STATE_FAILED,
    STATE_FILLED,
    STATE_SUBMITTED,
    ExecutionResult,
    OrderIntent,
)
from polytempo.storage.live_postgres import (
    LiveEventRow,
    get_live_connection,
    initialize_live_database,
    insert_live_event,
    truncate_live_tables,
)

LIVE_TEST_DB = os.environ.get("POLYTEMPO_LIVE_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not LIVE_TEST_DB, reason="Set POLYTEMPO_LIVE_TEST_DATABASE_URL for live ledger tests"
)


@pytest.fixture
def ledger() -> LiveLedger:
    url = LIVE_TEST_DB
    assert url is not None
    initialize_live_database(url)
    with get_live_connection(url) as conn:
        truncate_live_tables(conn)
        conn.commit()
    return LiveLedger(url)


def _intent(
    *,
    intent_id: str = "i1",
    event_id: str = "evt1",
    bucket_label: str = "24C",
    market_side: str = SIDE_YES,
    token_id: str = "tokY",
    limit_price: float = 0.30,
    shares: float = 100.0,
    stake_usd: float = 30.0,
    metadata: dict | None = None,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        event_id=event_id,
        bucket_label=bucket_label,
        token_id=token_id,
        market_side=market_side,
        limit_price=limit_price,
        shares=shares,
        stake_usd=stake_usd,
        knob_id="knob1",
        mode=MODE_DRY_RUN,
        ts_utc="2026-07-18T10:00:00+00:00",
        edge_pp=8.0,
        lead_hours=30.0,
        metadata=metadata or {},
    )


def test_full_flow_intent_order_result_settle(ledger: LiveLedger) -> None:
    intent = _intent()
    ledger.record_intent(intent)

    # in-flight intent counts as exposure and as open on the event
    assert ledger.has_open_on_event("evt1") is True
    assert ledger.open_exposure_usd() == pytest.approx(30.0)
    assert ledger.event_exposure_usd("evt1") == pytest.approx(30.0)

    unresolved = ledger.unresolved_intents()
    assert len(unresolved) == 1
    assert unresolved[0]["intent_id"] == "i1"
    assert unresolved[0]["order_id"] is None
    assert unresolved[0]["state"] is None

    ledger.record_order_state(intent, "o1", STATE_SUBMITTED)
    ledger.record_order_state(intent, "o1", STATE_FILLED, filled_shares=100.0, avg_fill_price=0.30)

    unresolved = ledger.unresolved_intents()
    assert unresolved[0]["order_id"] == "o1"
    assert unresolved[0]["state"] == STATE_FILLED

    ledger.record_result(
        ExecutionResult(
            intent=intent,
            state=STATE_FILLED,
            order_id="o1",
            filled_shares=100.0,
            avg_fill_price=0.30,
        )
    )

    assert ledger.unresolved_intents() == []
    assert ledger.open_exposure_usd() == pytest.approx(30.0)  # filled RESULT stake
    assert ledger.open_event_ids() == ["evt1"]

    settled = ledger.record_settlement("evt1", "24C", ts_utc="2026-07-18T12:00:00+00:00")
    assert settled == ["i1"]

    assert ledger.has_open_on_event("evt1") is False
    assert ledger.open_event_ids() == []
    assert ledger.open_exposure_usd() == pytest.approx(0.0)
    # payout (100 shares win) minus RESULT stake (30) = 70
    assert ledger.realized_pnl_on_day("2026-07-18") == pytest.approx(70.0)


def test_settlement_idempotent(ledger: LiveLedger) -> None:
    intent = _intent()
    ledger.record_intent(intent)
    ledger.record_result(
        ExecutionResult(intent, STATE_FILLED, "o1", 100.0, 0.30)
    )
    first = ledger.record_settlement("evt1", "24C")
    second = ledger.record_settlement("evt1", "24C")
    assert first == ["i1"]
    assert second == []


def test_losing_yes_settlement_pays_zero(ledger: LiveLedger) -> None:
    intent = _intent(bucket_label="24C", market_side=SIDE_YES)
    ledger.record_intent(intent)
    ledger.record_result(ExecutionResult(intent, STATE_FILLED, "o1", 100.0, 0.30))
    ledger.record_settlement("evt1", "25C", ts_utc="2026-07-18T12:00:00+00:00")
    # lost: payout 0 minus stake 30
    assert ledger.realized_pnl_on_day("2026-07-18") == pytest.approx(-30.0)


def test_no_side_wins_when_bucket_differs(ledger: LiveLedger) -> None:
    intent = _intent(bucket_label="24C", market_side=SIDE_NO, token_id="tokN")
    ledger.record_intent(intent)
    ledger.record_result(ExecutionResult(intent, STATE_FILLED, "o1", 100.0, 0.30))
    ledger.record_settlement("evt1", "25C", ts_utc="2026-07-18T12:00:00+00:00")
    # NO wins because 24C != winning 25C: payout 100 minus stake 30
    assert ledger.realized_pnl_on_day("2026-07-18") == pytest.approx(70.0)


def test_exposure_mixes_filled_and_inflight(ledger: LiveLedger) -> None:
    filled = _intent(intent_id="i1", stake_usd=30.0)
    inflight = _intent(intent_id="i2", stake_usd=15.0, token_id="tokY2")
    ledger.record_intent(filled)
    ledger.record_intent(inflight)
    ledger.record_result(ExecutionResult(filled, STATE_FILLED, "o1", 100.0, 0.30))

    # filled RESULT stake (100*0.30=30) + in-flight INTENT stake (15) = 45
    assert ledger.open_exposure_usd() == pytest.approx(45.0)
    assert ledger.event_exposure_usd("evt1") == pytest.approx(45.0)
    assert ledger.event_exposure_usd("other") == pytest.approx(0.0)

    positions = ledger.open_positions_by_token()
    assert positions == {"tokY": pytest.approx(100.0)}


def test_zero_fill_terminal_does_not_block_event(ledger: LiveLedger) -> None:
    intent = _intent()
    ledger.record_intent(intent)
    assert ledger.has_open_on_event("evt1") is True  # in-flight blocks

    ledger.record_result(
        ExecutionResult(intent, STATE_CANCELED, "o1", 0.0, None)
    )
    # canceled unfilled: a later gate may try the event again
    assert ledger.has_open_on_event("evt1") is False


def test_has_open_on_event_scopes_by_profile(ledger: LiveLedger) -> None:
    intent = _intent(metadata={"profile_id": "live_k_lead42"})
    ledger.record_intent(intent)
    ledger.record_result(ExecutionResult(intent, STATE_FILLED, "o1", 100.0, 0.30))

    assert ledger.has_open_on_event("evt1", "live_k_lead42") is True
    assert ledger.has_open_on_event("evt1", "live_k_lead30") is False
    assert ledger.has_open_on_event("evt1") is True  # unscoped sees any profile


_BEFORE = "2026-07-18T00:00:00+00:00"  # earlier than _intent()'s ts_utc


def test_retry_pending_after_unfilled_cancel(ledger: LiveLedger) -> None:
    intent = _intent(metadata={"profile_id": "live_k_lead24"})
    ledger.record_intent(intent)
    ledger.record_result(ExecutionResult(intent, STATE_CANCELED, "o1", 0.0, None))

    assert ledger.unfilled_retry_pending("live_k_lead24", _BEFORE) is True
    assert ledger.unfilled_retry_pending("live_other_lead24", _BEFORE) is False


def test_retry_pending_after_placement_failure(ledger: LiveLedger) -> None:
    """A 503 never reaches the book: no order id, safe to re-send."""
    intent = _intent(metadata={"profile_id": "live_k_lead24"})
    ledger.record_intent(intent)
    ledger.record_result(ExecutionResult(intent, STATE_FAILED, None, 0.0, None))

    assert ledger.unfilled_retry_pending("live_k_lead24", _BEFORE) is True


def test_no_retry_when_failed_order_may_be_resting(ledger: LiveLedger) -> None:
    """FAILED *with* an order id means unknown state — never re-send."""
    intent = _intent(metadata={"profile_id": "live_k_lead24"})
    ledger.record_intent(intent)
    ledger.record_result(ExecutionResult(intent, STATE_FAILED, "o1", 0.0, None))

    assert ledger.unfilled_retry_pending("live_k_lead24", _BEFORE) is False


def test_no_retry_once_anything_filled(ledger: LiveLedger) -> None:
    """One unfilled bucket does not license a retry when another one filled."""
    unfilled = _intent(intent_id="i1", metadata={"profile_id": "live_k_lead24"})
    filled = _intent(
        intent_id="i2", bucket_label="25C", metadata={"profile_id": "live_k_lead24"}
    )
    ledger.record_intent(unfilled)
    ledger.record_result(ExecutionResult(unfilled, STATE_CANCELED, "o1", 0.0, None))
    ledger.record_intent(filled)
    ledger.record_result(ExecutionResult(filled, STATE_FILLED, "o2", 100.0, 0.30))

    assert ledger.unfilled_retry_pending("live_k_lead24", _BEFORE) is False


def test_no_retry_outside_the_window(ledger: LiveLedger) -> None:
    intent = _intent(metadata={"profile_id": "live_k_lead24"})
    ledger.record_intent(intent)
    ledger.record_result(ExecutionResult(intent, STATE_CANCELED, "o1", 0.0, None))

    assert ledger.unfilled_retry_pending("live_k_lead24", "2026-07-19T00:00:00+00:00") is False


def test_no_retry_while_intent_still_in_flight(ledger: LiveLedger) -> None:
    """An INTENT with no RESULT is mid-attempt, not a finished failure."""
    intent = _intent(metadata={"profile_id": "live_k_lead24"})
    ledger.record_intent(intent)

    assert ledger.unfilled_retry_pending("live_k_lead24", _BEFORE) is False


def test_resolve_stale_intent_writes_terminal(ledger: LiveLedger) -> None:
    intent = _intent()
    ledger.record_intent(intent)
    ledger.record_order_state(intent, "o1", STATE_SUBMITTED)

    unresolved = ledger.unresolved_intents()
    assert unresolved[0]["order_id"] == "o1"
    assert unresolved[0]["state"] == STATE_SUBMITTED

    ledger.resolve_stale_intent("i1", STATE_FAILED, message="reconciled")

    assert ledger.unresolved_intents() == []
    assert ledger.open_exposure_usd() == pytest.approx(0.0)  # RESULT filled_shares 0


def test_halt_clear_cycle(ledger: LiveLedger) -> None:
    assert ledger.is_halted() is False
    ledger.halt("daily loss limit")
    assert ledger.is_halted() is True
    ledger.clear_halt()
    assert ledger.is_halted() is False
    ledger.halt("kill switch")
    assert ledger.is_halted() is True


def test_nan_metadata_survives_insert(ledger: LiveLedger) -> None:
    url = ledger._database_url
    row = LiveEventRow(
        event_type="INTENT",
        ts_utc="2026-07-18T10:00:00+00:00",
        intent_id="nan-intent",
        metadata={
            "sigma_c": float("nan"),
            "mu_c": float("inf"),
            "nested": {"neg": float("-inf"), "ok": 1.5},
            "arr": [float("nan"), 2.0],
        },
    )
    with get_live_connection(url) as conn:
        insert_live_event(conn, row)  # must not raise on NaN -> jsonb
        conn.commit()
        stored = conn.execute(
            "SELECT metadata FROM live_events WHERE intent_id = 'nan-intent'"
        ).fetchone()["metadata"]

    assert stored["sigma_c"] is None
    assert stored["mu_c"] is None
    assert stored["nested"] == {"neg": None, "ok": 1.5}
    assert stored["arr"] == [None, 2.0]


def test_record_intent_with_nan_metadata(ledger: LiveLedger) -> None:
    intent = _intent(metadata={"distribution_sigma_c": float("nan")})
    ledger.record_intent(intent)  # must not raise
    assert math.isnan(intent.metadata["distribution_sigma_c"])  # source unchanged
    unresolved = ledger.unresolved_intents()
    assert len(unresolved) == 1
