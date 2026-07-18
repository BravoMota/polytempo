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
    python scripts/backtest.py --start 2026-06-01 --end 2026-06-20 \\
        --trade-strategy dist_arb --event-budget budget_normalize_wallet_percent --csv out.csv
    python scripts/backtest.py --start 2026-06-01 --end 2026-06-20 \\
        --trade-strategy dist_arb --csv reports/backtest/dist_arb_summary.csv \\
        --daily-csv reports/backtest/dist_arb_daily.csv
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

import yaml  # noqa: E402

from polytempo.analysis import MODEL_STRATEGIES  # noqa: E402
from polytempo.paper.backtest import (  # noqa: E402
    BacktestResult,
    ProfileBacktestResult,
    build_daily_performance_rows,
    run_backtest,
)
from polytempo.paper.performance_csv import write_performance_daily_csv  # noqa: E402
from polytempo.profiles.load import (  # noqa: E402
    expand_event_budgets,
    generate_all_twelve_profiles,
    load_paper_profiles,
    normalize_event_budget,
    parse_event_budget_fraction,
    parse_event_budgets,
)
from polytempo.profiles.models import TradingProfile  # noqa: E402
from polytempo.profiles.registry import known_trade_strategies  # noqa: E402

logger = logging.getLogger(__name__)

# Research grid only — never the live paper bot default.
DEFAULT_BACKTEST_PROFILES_PATH = Path("config/backtest_profiles.yaml")


def _parse_date(text: str) -> date:
    return date.fromisoformat(text)


def _normalize_event_budget(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        return normalize_event_budget(raw)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _select_profiles(
    profiles: list[TradingProfile],
    *,
    ids: list[str] | None,
    trade_strategy: str | None,
    model_strategy: str | None,
    event_budget: str | None,
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
    if event_budget:
        selected = [p for p in selected if p.sizing_mode == event_budget]
    return selected


def _synthesize_profiles(
    config: Path,
    *,
    trade_strategy: str | None,
    model_strategy: str | None,
    lead_gate_keys: list[str] | None,
    event_budget: str | None,
) -> list[TradingProfile]:
    """Build hold-to-settlement profiles for a strategy selection, in-memory.

    Reads lead gates / city / calibration / strategy lists from ``config``.
    ``--event-budget`` locks the capital-allocation strategy
    (``legacy`` / ``budget_normalize_wallet_percent``); omit to use the
    config's ``event_budgets`` list.

    Never writes to the paper ledger.
    """
    if not config.is_file():
        raise SystemExit(f"config not found: {config}")
    raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}

    lead_gates = raw.get("lead_gates") or {}
    if not lead_gates:
        raise SystemExit(f"no lead_gates defined in {config}")
    if lead_gate_keys:
        unknown = [k for k in lead_gate_keys if k not in lead_gates]
        if unknown:
            raise SystemExit(
                f"unknown lead gate(s): {unknown}; known: {sorted(lead_gates)}"
            )
        lead_gates = {k: lead_gates[k] for k in lead_gate_keys}

    if model_strategy:
        model_list = [model_strategy]
    else:
        model_list = list(raw.get("model_strategies") or MODEL_STRATEGIES)
    if trade_strategy:
        trade_list = [trade_strategy]
    else:
        trade_list = list(raw.get("trade_strategies") or known_trade_strategies())

    unknown_trades = sorted(set(trade_list) - set(known_trade_strategies()))
    if unknown_trades:
        raise SystemExit(
            f"unknown trade strategy(ies): {unknown_trades}; "
            f"known: {sorted(known_trade_strategies())}"
        )

    kwargs: dict = {}
    if "calibration_stats_path" in raw:
        kwargs["calibration_stats_path"] = Path(raw["calibration_stats_path"])
    if "updated_calibration_stats_path" in raw:
        kwargs["updated_calibration_stats_path"] = Path(
            raw["updated_calibration_stats_path"]
        )

    legacy = generate_all_twelve_profiles(
        lead_gates=lead_gates,
        model_strategies=model_list,
        trade_strategies=trade_list,
        city=str(raw.get("city", "london")),
        target_day=str(raw.get("target_day", "tomorrow")),
        **kwargs,
    )
    budgets = [event_budget] if event_budget else parse_event_budgets(raw)
    return expand_event_budgets(
        legacy,
        event_budgets=budgets,
        event_budget_fraction=parse_event_budget_fraction(raw),
    )


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


def _write_daily_csv(
    result: BacktestResult, profiles: list[TradingProfile], path: Path
) -> None:
    rows = build_daily_performance_rows(result, profiles)
    write_performance_daily_csv(path, rows)
    print(f"\nWrote {len(rows)} daily row(s) to {path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Hold-to-settlement paper trading backtest (no DB writes)"
    )
    parser.add_argument("--start", required=True, type=_parse_date, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, type=_parse_date, help="YYYY-MM-DD")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_BACKTEST_PROFILES_PATH,
        help=(
            "Profile YAML for this backtest (default: config/backtest_profiles.yaml). "
            "Not the live paper bot config — use config/paper_profiles.yaml only if "
            "you intentionally want the production wallet grid."
        ),
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
        help=(
            "Backtest this trade strategy (any registered name, even one not in "
            "the config YAML). Combined with the config's model strategies "
            "unless --model-strategy is also given. Never touches the ledger."
        ),
    )
    parser.add_argument(
        "--model-strategy",
        default=None,
        choices=list(MODEL_STRATEGIES),
        help=(
            "Backtest this distribution model strategy. Combined with the "
            "config's trade strategies unless --trade-strategy is also given. "
            "Never touches the ledger."
        ),
    )
    parser.add_argument(
        "--lead-gates",
        nargs="+",
        default=None,
        help="Restrict to these lead gate keys (e.g. lead24 lead42)",
    )
    parser.add_argument(
        "--event-budget",
        default=None,
        help=(
            "Lock the event_budget strategy knob: legacy or "
            "budget_normalize_wallet_percent (alias: bnwp). "
            "Omit to use event_budgets from the config YAML."
        ),
    )
    parser.add_argument(
        "--sizing-mode",
        default=None,
        help="Deprecated alias for --event-budget",
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
        "--daily-csv",
        type=Path,
        default=None,
        help=(
            "Write visualizer-compatible daily CSV (profile × settlement_date). "
            "Use a path under reports/backtest/ so Refresh-from-DB does not "
            "overwrite reports/performance/daily.csv"
        ),
    )
    parser.add_argument(
        "--daily", action="store_true", help="Also print per-day breakdown"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )
    # Keep progress lines visible promptly when stdout/stderr are redirected to a file.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(line_buffering=True)

    raw_budget = args.event_budget if args.event_budget is not None else args.sizing_mode
    event_budget = _normalize_event_budget(raw_budget)

    if args.profiles:
        all_profiles = load_paper_profiles(args.config)
        profiles = _select_profiles(
            all_profiles,
            ids=args.profiles,
            trade_strategy=args.trade_strategy,
            model_strategy=args.model_strategy,
            event_budget=event_budget,
        )
    elif args.trade_strategy or args.model_strategy or args.lead_gates:
        profiles = _synthesize_profiles(
            args.config,
            trade_strategy=args.trade_strategy,
            model_strategy=args.model_strategy,
            lead_gate_keys=args.lead_gates,
            event_budget=event_budget,
        )
    else:
        all_profiles = load_paper_profiles(args.config)
        profiles = _select_profiles(
            all_profiles,
            ids=None,
            trade_strategy=None,
            model_strategy=None,
            event_budget=event_budget,
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
    if args.daily_csv is not None:
        _write_daily_csv(result, profiles, args.daily_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
