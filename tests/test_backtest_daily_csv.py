"""Tests for backtest visualizer-compatible daily CSV export."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "src"))

import backtest as backtest_cli  # noqa: E402
from polytempo.paper.backtest import (  # noqa: E402
    BacktestResult,
    BacktestTrade,
    ProfileBacktestResult,
    build_daily_performance_rows,
)
from polytempo.paper.performance_csv import (  # noqa: E402
    PERFORMANCE_DAILY_CSV_COLUMNS,
    write_performance_daily_csv,
)
from polytempo.profiles.models import EntryGate, TradingProfile  # noqa: E402
from polytempo.storage.paper_postgres import STARTING_BALANCE_USD  # noqa: E402


def _trade(
    *,
    profile_id: str,
    settlement_date: date,
    stake_usd: float,
    payout_usd: float,
    won: bool | None = None,
) -> BacktestTrade:
    if won is None:
        won = payout_usd > 0
    return BacktestTrade(
        profile_id=profile_id,
        event_id=f"evt-{settlement_date.isoformat()}",
        settlement_date=settlement_date,
        bucket_label="24°C",
        side="YES",
        entry_price=0.4,
        stake_usd=stake_usd,
        shares=payout_usd if won else stake_usd / 0.4,
        edge_pp=5.0,
        won=won,
        payout_usd=payout_usd,
    )


def _profile(profile_id: str = "bh_dist_arb_lead24") -> TradingProfile:
    return TradingProfile(
        id=profile_id,
        model_strategy="best_historical",
        trade_strategy="dist_arb",
        entry_gate=EntryGate(target_lead_hours=24.0, tolerance_seconds=90.0),
        city="london",
        target_day="tomorrow",
    )


def test_build_daily_performance_rows_sod_and_compounding() -> None:
    d1 = date(2026, 6, 20)
    d2 = date(2026, 6, 21)
    profile = _profile()
    result = BacktestResult(
        start=d1,
        end=d2,
        profiles={
            profile.id: ProfileBacktestResult(
                profile_id=profile.id,
                trades=[
                    _trade(
                        profile_id=profile.id,
                        settlement_date=d1,
                        stake_usd=50.0,
                        payout_usd=100.0,
                    ),
                    _trade(
                        profile_id=profile.id,
                        settlement_date=d2,
                        stake_usd=40.0,
                        payout_usd=0.0,
                        won=False,
                    ),
                ],
            )
        },
    )
    rows = build_daily_performance_rows(result, [profile])
    assert len(rows) == 2
    assert list(rows[0].keys()) == PERFORMANCE_DAILY_CSV_COLUMNS

    day1 = rows[0]
    assert day1["settlement_date"] == "2026-06-20"
    assert day1["n_trades"] == 1
    assert day1["pnl_usd"] == 50.0
    assert day1["sod_balance_usd"] == pytest.approx(STARTING_BALANCE_USD - 50.0)
    assert day1["pnl_pct"] == pytest.approx(
        round(100.0 * 50.0 / (STARTING_BALANCE_USD - 50.0), 4)
    )
    assert day1["model"] == "best_historical"
    assert day1["trade"] == "dist_arb"
    assert day1["lead_hours"] == "24"
    assert day1["exit_mode"] == "hold"
    assert day1["sizing_mode"] == "legacy"
    assert day1["since"] == "2026-06-20"

    day2 = rows[1]
    assert day2["settlement_date"] == "2026-06-21"
    assert day2["pnl_usd"] == -40.0
    # After day1: balance = 1000 + 50 = 1050; sod = 1050 - 40
    assert day2["sod_balance_usd"] == pytest.approx(1010.0)
    assert day2["pnl_pct"] == pytest.approx(round(100.0 * -40.0 / 1010.0, 4))
    assert day2["since"] == "2026-06-20"


def test_build_daily_performance_rows_skips_empty_profiles() -> None:
    profile = _profile("empty_wallet")
    result = BacktestResult(
        start=date(2026, 6, 20),
        end=date(2026, 6, 20),
        profiles={
            profile.id: ProfileBacktestResult(profile_id=profile.id, trades=[]),
        },
    )
    assert build_daily_performance_rows(result, [profile]) == []


def test_write_performance_daily_csv_header(tmp_path: Path) -> None:
    out = tmp_path / "daily.csv"
    write_performance_daily_csv(
        out,
        [
            {
                "profile_id": "p1",
                "model": "best_historical",
                "trade": "dist_arb",
                "lead_hours": "24",
                "exit_mode": "hold",
                "sizing_mode": "legacy",
                "since": "2026-06-20",
                "settlement_date": "2026-06-20",
                "pnl_usd": 10.0,
                "pnl_pct": 1.0,
                "sod_balance_usd": 990.0,
                "n_trades": 1,
            }
        ],
    )
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# generated_utc:")
    assert ",".join(PERFORMANCE_DAILY_CSV_COLUMNS) in text


def test_cli_daily_csv_writes_file(tmp_path: Path) -> None:
    profile = _profile()
    d1 = date(2026, 6, 20)
    fake_result = BacktestResult(
        start=d1,
        end=d1,
        profiles={
            profile.id: ProfileBacktestResult(
                profile_id=profile.id,
                trades=[
                    _trade(
                        profile_id=profile.id,
                        settlement_date=d1,
                        stake_usd=50.0,
                        payout_usd=100.0,
                    )
                ],
            )
        },
        events_processed=1,
    )
    daily_path = tmp_path / "daily.csv"
    summary_path = tmp_path / "summary.csv"

    with (
        patch.object(backtest_cli, "load_paper_profiles", return_value=[profile]),
        patch.object(backtest_cli, "run_backtest", return_value=fake_result),
        patch.object(
            sys,
            "argv",
            [
                "backtest.py",
                "--start",
                "2026-06-20",
                "--end",
                "2026-06-20",
                "--profiles",
                profile.id,
                "--csv",
                str(summary_path),
                "--daily-csv",
                str(daily_path),
            ],
        ),
    ):
        assert backtest_cli.main() == 0

    assert summary_path.is_file()
    assert daily_path.is_file()
    daily_text = daily_path.read_text(encoding="utf-8")
    assert daily_text.startswith("# generated_utc:")
    assert ",".join(PERFORMANCE_DAILY_CSV_COLUMNS) in daily_text
    assert profile.id in daily_text
