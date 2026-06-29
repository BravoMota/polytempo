"""CSV loading and export helpers for the performance viewer."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from polytempo.visualizer.paths import EXPORT_SCRIPT, REPO_ROOT, RUN_WITH_ENV


def csv_mtime(path: Path) -> float:
    if not path.is_file():
        return -1.0
    return path.stat().st_mtime


def read_csv_raw(path: Path) -> tuple[pd.DataFrame, str | None]:
    """Return dataframe and optional generated_utc from comment line."""
    generated: str | None = None
    with path.open(encoding="utf-8") as fh:
        first = fh.readline()
        if first.startswith("# generated_utc:"):
            generated = first.split(":", 1)[1].strip()
    df = pd.read_csv(path, comment="#")
    df["settlement_date"] = pd.to_datetime(df["settlement_date"]).dt.date
    if "since" in df.columns:
        df["since"] = pd.to_datetime(df["since"], errors="coerce").dt.date
    df["lead_hours"] = df["lead_hours"].apply(
        lambda v: "" if pd.isna(v) or str(v).strip() == "" else str(int(float(v)))
    )
    return df, generated


def _use_env_wrapper() -> bool:
    """The bash wrapper (sources .env, picks venv python) only runs on POSIX."""
    return os.name != "nt" and RUN_WITH_ENV.is_file()


def export_command(csv_path: Path) -> list[str]:
    head = (
        [str(RUN_WITH_ENV), str(EXPORT_SCRIPT)]
        if _use_env_wrapper()
        else [sys.executable, str(EXPORT_SCRIPT)]
    )
    return [*head, "--all", "--csv", str(csv_path)]


def run_export(csv_path: Path) -> tuple[bool, str]:
    if not EXPORT_SCRIPT.is_file():
        return False, f"Missing {EXPORT_SCRIPT}"
    if os.name != "nt" and not RUN_WITH_ENV.is_file():
        return False, f"Missing {RUN_WITH_ENV}"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        export_command(csv_path),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "export failed").strip()
        tail = "\n".join(err.splitlines()[-20:])
        return False, tail
    msg = (result.stdout or "export ok").strip()
    return True, msg


def fmt_pct(v: float) -> str:
    if pd.isna(v):
        return "·"
    if abs(v) < 0.05:
        return "0.0"
    return f"{v:+.1f}"


def period_pnl_pct(sub: pd.DataFrame) -> float:
    sod = float(sub["sod_balance_usd"].sum())
    if sod <= 0:
        return 0.0
    return 100.0 * float(sub["pnl_usd"].sum()) / sod


def visible_settlement_dates(filtered: pd.DataFrame) -> list[date]:
    return sorted(filtered["settlement_date"].unique())


def wallet_settlement_dates(
    filtered: pd.DataFrame,
    wallet: str,
    visible_dates: list[date],
) -> list[date]:
    """Dates with a realization for this wallet in the filtered CSV."""
    sub = filtered[
        (filtered["profile_id"] == wallet) & (filtered["n_trades"] > 0)
    ]
    active = set(sub["settlement_date"].tolist())
    return [d for d in visible_dates if d in active]


def detail_context_from_filtered(
    filtered: pd.DataFrame,
    wallet: str,
    settlement_date: date,
) -> str | None:
    """CSV cross-check line for wallet × settlement date."""
    sub = filtered[
        (filtered["profile_id"] == wallet)
        & (filtered["settlement_date"] == settlement_date)
    ]
    if sub.empty:
        return None
    row = sub.iloc[0]
    n_trades = int(row.get("n_trades", 0) or 0)
    if n_trades <= 0:
        return None
    pnl_usd = float(row["pnl_usd"])
    pnl_pct = float(row["pnl_pct"])
    return (
        f"CSV: {n_trades} trade(s), "
        f"P/L ${pnl_usd:+.2f} ({fmt_pct(pnl_pct)}% of SOD balance)"
    )
