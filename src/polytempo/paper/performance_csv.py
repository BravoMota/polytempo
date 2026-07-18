"""Shared long-format daily performance CSV (visualizer / report_performance)."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

PERFORMANCE_DAILY_CSV_COLUMNS = [
    "profile_id",
    "model",
    "trade",
    "lead_hours",
    "exit_mode",
    "event_budget",
    "since",
    "settlement_date",
    "pnl_usd",
    "pnl_pct",
    "sod_balance_usd",
    "n_trades",
]


def write_performance_daily_csv(path: Path, rows: list[dict]) -> None:
    """Write visualizer-compatible daily rows with a generated_utc comment."""
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(f"# generated_utc: {generated}\n")
        writer = csv.DictWriter(fh, fieldnames=PERFORMANCE_DAILY_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
