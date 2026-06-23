"""Tests for polytempo.visualizer.trades."""

from __future__ import annotations

from datetime import date

from polytempo.visualizer.trades import (
    group_by_event,
    realized_trades_from_rows,
)


def _open_row(
    *,
    trade_id: str,
    event_id: str,
    stake: float,
    ts: str = "2026-06-15T18:00:00+00:00",
) -> dict:
    return {
        "event_type": "OPEN",
        "trade_id": trade_id,
        "ts_utc": ts,
        "polymarket_event_id": event_id,
        "bucket_label": "26°C",
        "side": "YES",
        "entry_price": 0.4,
        "stake_usd": stake,
        "shares": stake / 0.4,
        "edge_pp": 5.0,
        "yes_bid": 0.38,
        "yes_ask": 0.4,
        "lead_hours": 42.0,
        "model_strategy": "best_historical",
        "trade_action": "BUY_YES",
        "metadata": {"selected_model": "ukmo_seamless"},
    }


def _settle_row(
    *,
    trade_id: str,
    event_id: str,
    payout: float,
    ts: str,
    winning_label: str = "26°C",
) -> dict:
    return {
        "event_type": "SETTLE",
        "trade_id": trade_id,
        "ts_utc": ts,
        "polymarket_event_id": event_id,
        "winning_label": winning_label,
        "outcome": "YES",
        "payout_usd": payout,
        "metadata": {},
    }


def _close_row(
    *,
    trade_id: str,
    event_id: str,
    payout: float,
    ts: str,
    reason: str = "TP",
) -> dict:
    return {
        "event_type": "CLOSE",
        "trade_id": trade_id,
        "ts_utc": ts,
        "polymarket_event_id": event_id,
        "outcome": reason,
        "payout_usd": payout,
        "metadata": {"reason": reason, "sell_price": 0.55},
    }


def test_realized_trades_use_settlement_date_not_ledger_timestamp() -> None:
    event_dates = {
        "evt-17": date(2026, 6, 17),
        "evt-19": date(2026, 6, 19),
    }
    rows = [
        _open_row(trade_id="t17", event_id="evt-17", stake=100.0),
        _settle_row(
            trade_id="t17",
            event_id="evt-17",
            payout=150.0,
            ts="2026-06-18T00:00:00+00:00",
        ),
        _open_row(
            trade_id="t19",
            event_id="evt-19",
            stake=50.0,
            ts="2026-06-17T18:00:00+00:00",
        ),
        _settle_row(
            trade_id="t19",
            event_id="evt-19",
            payout=0.0,
            ts="2026-06-20T00:00:00+00:00",
        ),
    ]
    june_17 = realized_trades_from_rows(
        rows, date(2026, 6, 17), event_settlement_dates=event_dates
    )
    june_19 = realized_trades_from_rows(
        rows, date(2026, 6, 19), event_settlement_dates=event_dates
    )
    assert len(june_17) == 1
    assert june_17[0].trade_id == "t17"
    assert june_17[0].pnl_usd == 50.0
    assert len(june_19) == 1
    assert june_19[0].trade_id == "t19"
    assert june_19[0].pnl_usd == -50.0


def test_close_buckets_with_market_settlement_date() -> None:
    event_dates = {"evt-19": date(2026, 6, 19)}
    rows = [
        _open_row(
            trade_id="t1",
            event_id="evt-19",
            stake=40.0,
            ts="2026-06-17T18:00:00+00:00",
        ),
        _close_row(
            trade_id="t1",
            event_id="evt-19",
            payout=44.0,
            ts="2026-06-19T14:00:00+00:00",
        ),
    ]
    trades = realized_trades_from_rows(
        rows, date(2026, 6, 19), event_settlement_dates=event_dates
    )
    assert len(trades) == 1
    assert trades[0].realization_type == "CLOSE"
    assert trades[0].pnl_usd == 4.0
    assert trades[0].exit_metadata["reason"] == "TP"


def test_group_by_event_resolution_label() -> None:
    event_dates = {"evt-a": date(2026, 6, 17), "evt-b": date(2026, 6, 17)}
    rows = [
        _open_row(trade_id="t1", event_id="evt-a", stake=10.0),
        _settle_row(
            trade_id="t1",
            event_id="evt-a",
            payout=20.0,
            ts="2026-06-17T23:00:00+00:00",
            winning_label="27°C",
        ),
        _open_row(trade_id="t2", event_id="evt-b", stake=15.0),
        _close_row(
            trade_id="t2",
            event_id="evt-b",
            payout=16.0,
            ts="2026-06-17T20:00:00+00:00",
        ),
    ]
    trades = realized_trades_from_rows(
        rows, date(2026, 6, 17), event_settlement_dates=event_dates
    )
    groups = group_by_event(trades)
    assert len(groups) == 2
    by_id = {g.polymarket_event_id: g for g in groups}
    assert by_id["evt-a"].resolution_label == "27°C"
    assert by_id["evt-b"].resolution_label == "early exit"
