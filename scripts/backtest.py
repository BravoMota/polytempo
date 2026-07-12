#!/usr/bin/env python3
"""Hold-to-settlement backtest harness (paper trading, no DB writes).

Walks historical London weather events between ``--start`` and ``--end`` and
simulates paper trading for each profile at its lead gate, reusing the live
``run_profile`` decision path and ledger bankroll math. Reads point-in-time
snapshots from the weather DB and the winning bucket from Gamma; it never
writes to ``polytempo_paper``.

Examples:
    python scripts/backtest.py --start 2026-06-01 --end 2026-06-20
    python scripts/backtest.py --start 2026-06-01 --end 2026-06-20 \\
        --profiles bh_max_edge_lead42 whu_dist_arb_lead24 --daily
    python scripts/backtest.py --start 2026-06-01 --end 2026-06-20 \\
        --trade-strategy dist_arb --csv out.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polytempo.analysis import MODEL_STRATEGIES  # noqa: E402
from polytempo.paper.backtest import (  # noqa: E402
    BacktestResult,
    ProfileBacktestResult,
    run_backtest,
)
from polytempo.profiles.load import (  # noqa: E402
    DEFAULT_PROFILES_PATH,
    load_paper_profiles,
)
from polytempo.profiles.models import TradingProfile  # noqa: E402

logger = logging.getLogger(__name__)


def _parse_date(text: str) -> date:
    return date.fromisoformat(text)


def _select_profiles(
    profiles: list[TradingProfile],
    *,
    ids: list[str] | None,
    trade_strategy: str | None,
    model_strategy: str | None,
) -> list[TradingProfile]:
    selected = [p for p in profiles if not p.is_active]
    if ids:
        wanted = set(ids)
        selected = [p for p in selected if p.id in wanted]
        missing = wanted - {p.id for p in selected}
        if missing:
            raise SystemExit(f"unknown profile id(s): {sorted(missing)}")
    if model_strategy:
        selected = [p for p in selected if p.model_strategy == model_strategy]
    if trade_strategy:
        selected = [p for p in selected if p.trade_strategy == trade_strategy]
    return selected


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _print_summary(result: BacktestResult) -> None:
    header = (
        f"{'profile':<28} {'trades':>7} {'wins':>6} {'win%':>7} "
        f"{'pnl$':>10} {'final$':>10} {'maxDD$':>9}"
    )
    print()
    print(
        f"Backtest {result.start.isoformat()} -> {result.end.isoformat()} "
        f"({result.events_processed} events, {len(result.profiles)} profiles)"
    )
    print(header)
    print("-" * len(header))
    for profile_id in sorted(result.profiles):
        r = result.profiles[profile_id]
        print(
            f"{profile_id:<28} {r.trade_count:>7} {r.win_count:>6} "
            f"{_fmt_pct(r.win_rate):>7} {r.pnl_usd:>10.2f} "
            f"{r.final_balance:>10.2f} {r.max_drawdown_usd:>9.2f}"
        )

    skips: dict[str, int] = {}
    for r in result.profiles.values():
        for reason, count in r.skips.items():
            skips[reason] = skips.get(reason, 0) + count
    if skips:
        print()
        print("No-open reasons (all profiles, summed over events):")
        for reason in sorted(skips, key=lambda k: -skips[k]):
            print(f"  {reason:<28} {skips[reason]:>6}")


def _print_daily(result: BacktestResult) -> None:
    breakdown = result.daily_breakdown()
    if not breakdown:
        return
    print()
    print("Daily breakdown (all profiles):")
    print(f"{'date':<12} {'trades':>7} {'pnl$':>10}")
    print("-" * 31)
    for day, stats in breakdown.items():
        print(
            f"{day.isoformat():<12} {int(stats['trades']):>7} "
            f"{stats['pnl_usd']:>10.2f}"
        )


def _write_csv(result: BacktestResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "profile_id",
                "trades",
                "wins",
                "win_rate",
                "starting_balance_usd",
                "final_balance_usd",
                "pnl_usd",
                "max_drawdown_usd",
            ]
        )
        for profile_id in sorted(result.profiles):
            r: ProfileBacktestResult = result.profiles[profile_id]
            writer.writerow(
                [
                    profile_id,
                    r.trade_count,
                    r.win_count,
                    "" if r.win_rate is None else round(r.win_rate, 4),
                    r.starting_balance,
                    round(r.final_balance, 2),
                    round(r.pnl_usd, 2),
                    round(r.max_drawdown_usd, 2),
                ]
            )
    print(f"\nWrote per-profile summary to {path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Hold-to-settlement paper trading backtest (no DB writes)"
    )
    parser.add_argument("--start", required=True, type=_parse_date, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, type=_parse_date, help="YYYY-MM-DD")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_PROFILES_PATH,
        help="Path to paper_profiles.yaml",
    )
    parser.add_argument(
        "--profiles",
        nargs="+",
        default=None,
        help="Restrict to these profile ids",
    )
    parser.add_argument(
        "--trade-strategy",
        default=None,
        help="Restrict to profiles with this trade strategy",
    )
    parser.add_argument(
        "--model-strategy",
        default=None,
        choices=list(MODEL_STRATEGIES),
        help="Restrict to profiles with this distribution model strategy",
    )
    parser.add_argument("--city", default="london", help="City (default: london)")
    parser.add_argument(
        "--database-url",
        default=None,
        help="Weather DB URL override (read-only; defaults to POLYTEMPO_DATABASE_URL)",
    )
    parser.add_argument(
        "--no-wunderground",
        action="store_true",
        help="Skip Wunderground snapshot forecast in the input reconstruction",
    )
    parser.add_argument("--csv", type=Path, default=None, help="Write summary CSV here")
    parser.add_argument(
        "--daily", action="store_true", help="Also print per-day breakdown"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    all_profiles = load_paper_profiles(args.config)
    profiles = _select_profiles(
        all_profiles,
        ids=args.profiles,
        trade_strategy=args.trade_strategy,
        model_strategy=args.model_strategy,
    )
    if not profiles:
        raise SystemExit("no profiles selected after filters")

    result = run_backtest(
        profiles,
        args.start,
        args.end,
        city=args.city,
        weather_database_url=args.database_url,
        use_wunderground=not args.no_wunderground,
    )

    _print_summary(result)
    if args.daily:
        _print_daily(result)
    if args.csv is not None:
        _write_csv(result, args.csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
