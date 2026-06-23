"""Tests for polytempo.visualizer.csv_data and paths."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from polytempo.visualizer.csv_data import (
    detail_context_from_filtered,
    export_command,
    run_export,
    wallet_settlement_dates,
)
from polytempo.visualizer.paths import REPO_ROOT


def test_export_command_args() -> None:
    csv_path = REPO_ROOT / "reports/performance/daily.csv"
    cmd = export_command(csv_path)
    assert cmd[0].endswith("run-with-env.sh")
    assert cmd[1].endswith("report_performance.py")
    assert "--all" in cmd
    assert "--csv" in cmd
    assert cmd[-1] == str(csv_path)


def test_run_export_success(tmp_path: Path) -> None:
    csv_path = tmp_path / "daily.csv"
    mock_result = MagicMock(returncode=0, stdout="wrote daily.csv (10 rows)\n", stderr="")
    with patch("polytempo.visualizer.csv_data.subprocess.run", return_value=mock_result) as run:
        ok, msg = run_export(csv_path)
    assert ok is True
    assert "10 rows" in msg
    run.assert_called_once()
    assert run.call_args.kwargs["cwd"] == REPO_ROOT


def test_detail_context_from_filtered() -> None:
    df = pd.DataFrame(
        [
            {
                "profile_id": "wallet_a",
                "settlement_date": date(2026, 6, 17),
                "pnl_usd": 12.5,
                "pnl_pct": 1.25,
                "n_trades": 2,
            },
            {
                "profile_id": "wallet_a",
                "settlement_date": date(2026, 6, 18),
                "pnl_usd": 0.0,
                "pnl_pct": 0.0,
                "n_trades": 0,
            },
        ]
    )
    line = detail_context_from_filtered(df, "wallet_a", date(2026, 6, 17))
    assert line is not None
    assert "2 trade" in line
    assert "+12.50" in line or "+12.5" in line
    assert detail_context_from_filtered(df, "wallet_a", date(2026, 6, 18)) is None


def test_wallet_settlement_dates_filters_zero_trade_days() -> None:
    df = pd.DataFrame(
        [
            {
                "profile_id": "w1",
                "settlement_date": date(2026, 6, 16),
                "n_trades": 1,
            },
            {
                "profile_id": "w1",
                "settlement_date": date(2026, 6, 17),
                "n_trades": 0,
            },
        ]
    )
    visible = [date(2026, 6, 16), date(2026, 6, 17)]
    assert wallet_settlement_dates(df, "w1", visible) == [date(2026, 6, 16)]
