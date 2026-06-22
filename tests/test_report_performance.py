"""Tests for scripts/report_performance.py settlement-date bucketing."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "report_performance.py"


def _load_report_module():
    spec = importlib.util.spec_from_file_location("report_performance", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["report_performance"] = module
    spec.loader.exec_module(module)
    return module


report = _load_report_module()


def test_realization_uses_settlement_date_not_ledger_timestamp() -> None:
    """Late/early UTC settles map to the weather day, not the ledger UTC day."""
    event_dates = {
        "evt-17": date(2026, 6, 17),
        "evt-19": date(2026, 6, 19),
    }
    rows = [
        {
            "profile_id": "p1",
            "event_type": "OPEN",
            "trade_id": "t17",
            "ts_utc": "2026-06-15T18:00:00+00:00",
            "stake_usd": 100.0,
            "payout_usd": None,
            "polymarket_event_id": "evt-17",
        },
        {
            "profile_id": "p1",
            "event_type": "SETTLE",
            "trade_id": "t17",
            "ts_utc": "2026-06-18T00:00:00+00:00",
            "stake_usd": None,
            "payout_usd": 150.0,
            "polymarket_event_id": "evt-17",
        },
        {
            "profile_id": "p1",
            "event_type": "OPEN",
            "trade_id": "t19",
            "ts_utc": "2026-06-17T18:00:00+00:00",
            "stake_usd": 50.0,
            "payout_usd": None,
            "polymarket_event_id": "evt-19",
        },
        {
            "profile_id": "p1",
            "event_type": "SETTLE",
            "trade_id": "t19",
            "ts_utc": "2026-06-20T00:00:00+00:00",
            "stake_usd": None,
            "payout_usd": 0.0,
            "polymarket_event_id": "evt-19",
        },
    ]
    window = {date(2026, 6, 17), date(2026, 6, 18), date(2026, 6, 19), date(2026, 6, 20)}
    perf = report._replay_profile(rows, window=window, event_settlement_dates=event_dates)
    assert perf is not None
    assert date(2026, 6, 17) in perf.daily
    assert date(2026, 6, 18) not in perf.daily
    assert date(2026, 6, 19) in perf.daily
    assert date(2026, 6, 20) not in perf.daily
    assert perf.daily[date(2026, 6, 17)].pnl_usd == 50.0
    assert perf.daily[date(2026, 6, 19)].pnl_usd == -50.0


def test_close_on_resolution_day_buckets_with_market_settlement_date() -> None:
    event_dates = {"evt-19": date(2026, 6, 19)}
    rows = [
        {
            "profile_id": "xsell",
            "event_type": "OPEN",
            "trade_id": "t1",
            "ts_utc": "2026-06-17T18:00:00+00:00",
            "stake_usd": 40.0,
            "payout_usd": None,
            "polymarket_event_id": "evt-19",
        },
        {
            "profile_id": "xsell",
            "event_type": "CLOSE",
            "trade_id": "t1",
            "ts_utc": "2026-06-19T14:00:00+00:00",
            "stake_usd": None,
            "payout_usd": 44.0,
            "polymarket_event_id": "evt-19",
        },
    ]
    perf = report._replay_profile(
        rows,
        window={date(2026, 6, 19)},
        event_settlement_dates=event_dates,
    )
    assert perf is not None
    assert perf.daily[date(2026, 6, 19)].pnl_usd == 4.0
