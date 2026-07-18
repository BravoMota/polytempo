#!/usr/bin/env python3
"""Daily realized P/L matrix for paper wallets (profiles).

Rows: one wallet per profile (or rolled up by trade/model). Columns: market
settlement dates (weather days, default last 7 ending ``--end``). Cell:
realized P/L for positions on that settlement date as % of balance immediately
before the first realization that day. ``SETTLE``/``CLOSE`` rows are bucketed by
the Polymarket event's settlement date (not the ledger timestamp), because Gamma
often resolves shortly after local end-of-day — e.g. 23:55Z on the prior UTC
date or 00:00Z the next UTC date.

Includes ``since`` (first OPEN) so new wallets are not compared on stale
cumulative totals alone.

Reads the live paper DB; no writes unless ``--out`` or ``--csv`` is set.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polytempo.paper.performance_csv import (  # noqa: E402
    PERFORMANCE_DAILY_CSV_COLUMNS,
    write_performance_daily_csv,
)
from polytempo.paper.settlement_reporting import (  # noqa: E402
    build_event_settlement_dates,
    realization_day,
)
from polytempo.profiles.load import DEFAULT_PROFILES_PATH, load_paper_profiles  # noqa: E402
from polytempo.storage.paper_postgres import (  # noqa: E402
    STARTING_BALANCE_USD,
    get_paper_connection,
    resolve_paper_database_url,
)


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# Alias kept for callers/tests that import CSV_COLUMNS from this module.
CSV_COLUMNS = PERFORMANCE_DAILY_CSV_COLUMNS


@dataclass(frozen=True)
class DayPnl:
    pnl_usd: float
    sod_balance_usd: float
    n_trades: int = 0

    @property
    def pct(self) -> float:
        if self.sod_balance_usd <= 0:
            return 0.0
        return 100.0 * self.pnl_usd / self.sod_balance_usd


@dataclass
class ProfilePerf:
    profile_id: str
    trade_strategy: str
    model_strategy: str
    since: date | None
    balance_usd: float
    daily: dict[date, DayPnl]
    lead_hours: float | None = None
    exit_mode: str = ""
    event_budget: str = ""


def _fetch_events(conn) -> list[dict]:
    return conn.execute(
        """
        SELECT id, profile_id, event_type, trade_id, ts_utc,
               stake_usd, payout_usd, polymarket_event_id
        FROM paper_events
        ORDER BY profile_id, id ASC
        """
    ).fetchall()


def _collect_event_ids(rows: list[dict]) -> set[str]:
    ids: set[str] = set()
    for row in rows:
        eid = row.get("polymarket_event_id")
        if eid:
            ids.add(str(eid))
    return ids


def _in_window(reporting_day: date, window: set[date] | None) -> bool:
    return window is None or reporting_day in window


def _replay_profile(
    rows: list[dict],
    *,
    window: set[date] | None,
    event_settlement_dates: dict[str, date],
) -> ProfilePerf | None:
    if not rows:
        return None

    profile_id = rows[0]["profile_id"]
    balance = STARTING_BALANCE_USD
    open_stakes: dict[str, float] = {}
    open_event_ids: dict[str, str] = {}
    daily_pnl_usd: dict[date, float] = defaultdict(float)
    daily_n_trades: dict[date, int] = defaultdict(int)
    sod_balance: dict[date, float] = {}
    since: date | None = None

    for row in rows:
        ts = _parse_ts(row["ts_utc"])
        if since is None and row["event_type"] == "OPEN":
            since = ts.date()

        kind = row["event_type"]
        if kind == "OPEN":
            stake = float(row["stake_usd"] or 0.0)
            balance -= stake
            trade_id = row["trade_id"]
            open_stakes[trade_id] = stake
            eid = row.get("polymarket_event_id")
            if eid:
                open_event_ids[trade_id] = str(eid)
        elif kind in ("SETTLE", "CLOSE"):
            trade_id = row["trade_id"]
            eid = row.get("polymarket_event_id") or open_event_ids.get(trade_id)
            reporting_day = realization_day(
                ts,
                polymarket_event_id=str(eid) if eid else None,
                event_settlement_dates=event_settlement_dates,
            )
            if reporting_day not in sod_balance:
                sod_balance[reporting_day] = balance
            stake = open_stakes.pop(trade_id, 0.0)
            open_event_ids.pop(trade_id, None)
            payout = float(row["payout_usd"] or 0.0)
            balance += payout
            if _in_window(reporting_day, window):
                daily_pnl_usd[reporting_day] += payout - stake
                daily_n_trades[reporting_day] += 1

    if window is None:
        day_keys = sorted(daily_pnl_usd)
    else:
        day_keys = [d for d in window if d in daily_pnl_usd]

    daily = {
        d: DayPnl(
            pnl_usd=daily_pnl_usd[d],
            sod_balance_usd=sod_balance.get(d, balance),
            n_trades=daily_n_trades[d],
        )
        for d in day_keys
    }
    return ProfilePerf(
        profile_id=profile_id,
        trade_strategy="",
        model_strategy="",
        since=since,
        balance_usd=round(balance, 2),
        daily=daily,
    )


def _attach_profile_meta(perfs: list[ProfilePerf], config: Path) -> list[ProfilePerf]:
    by_id = {p.id: p for p in load_paper_profiles(config)}
    out: list[ProfilePerf] = []
    for perf in perfs:
        meta = by_id.get(perf.profile_id)
        if meta is None:
            out.append(perf)
            continue
        if meta.active_params is not None:
            exit_mode = "active"
            lead_hours = None  # active wallets span every lead tick
        elif meta.exit_policy is not None:
            exit_mode = "xsell"
            lead_hours = meta.entry_gate.target_lead_hours
        else:
            exit_mode = "hold"
            lead_hours = meta.entry_gate.target_lead_hours
        out.append(
            ProfilePerf(
                profile_id=perf.profile_id,
                trade_strategy=meta.trade_strategy,
                model_strategy=meta.model_strategy,
                since=perf.since,
                balance_usd=perf.balance_usd,
                daily=perf.daily,
                lead_hours=lead_hours,
                exit_mode=exit_mode,
                event_budget=meta.sizing_mode,
            )
        )
    return out


def _rollup(
    perfs: list[ProfilePerf],
    *,
    key_fn,
    label_fn,
) -> list[ProfilePerf]:
    groups: dict[str, list[ProfilePerf]] = defaultdict(list)
    for p in perfs:
        groups[key_fn(p)].append(p)

    rolled: list[ProfilePerf] = []
    for key, members in groups.items():
        since = min((m.since for m in members if m.since), default=None)
        balance = sum(m.balance_usd for m in members)
        daily: dict[date, DayPnl] = {}
        for d in {day for m in members for day in m.daily}:
            pnl = sum(m.daily[d].pnl_usd for m in members if d in m.daily)
            sod = sum(
                m.daily[d].sod_balance_usd for m in members if d in m.daily
            ) or (len(members) * STARTING_BALANCE_USD)
            n_trades = sum(m.daily[d].n_trades for m in members if d in m.daily)
            daily[d] = DayPnl(pnl_usd=pnl, sod_balance_usd=sod, n_trades=n_trades)
        rolled.append(
            ProfilePerf(
                profile_id=label_fn(key, members),
                trade_strategy=members[0].trade_strategy if len(members) == 1 else "",
                model_strategy=members[0].model_strategy if len(members) == 1 else "",
                since=since,
                balance_usd=round(balance, 2),
                daily=daily,
            )
        )
    return rolled


def _window_days(*, days: int, end: date) -> list[date]:
    return [end - timedelta(days=offset) for offset in range(days - 1, -1, -1)]


def _sum_window_pct(perf: ProfilePerf, window: list[date]) -> float:
    """Sum of the row's daily percentages over the window (Σ of the cells)."""
    return sum(perf.daily[d].pct for d in window if d in perf.daily)


def _fmt_cell(perf: ProfilePerf, d: date) -> str:
    if d not in perf.daily:
        return "·"
    pct = perf.daily[d].pct
    if abs(pct) < 0.05:
        return "0.0"
    return f"{pct:+.1f}"


def _render_table(
    perfs: list[ProfilePerf],
    *,
    window: list[date],
    title: str,
) -> str:
    day_headers = [d.strftime("%m-%d") for d in window]
    header = (
        f"| wallet | since | bal | {len(window)}d Σ% | {' | '.join(day_headers)} |"
    )
    sep = (
        "|---|---:|---:|---:|" + "|".join(["---:"] * len(window)) + "|"
    )
    lines = [f"## {title}", "", header, sep]
    for perf in perfs:
        since_s = perf.since.isoformat() if perf.since else "—"
        sum_pct = _sum_window_pct(perf, window)
        cells = " | ".join(_fmt_cell(perf, d) for d in window)
        lines.append(
            f"| {perf.profile_id} | {since_s} | ${perf.balance_usd:.0f} "
            f"| {sum_pct:+.1f} | {cells} |"
        )
    return "\n".join(lines)


def _load_perfs(
    *,
    window: set[date] | None,
    config: Path,
) -> list[ProfilePerf]:
    url = resolve_paper_database_url()
    with get_paper_connection(url) as conn:
        rows = _fetch_events(conn)

    event_settlement_dates = build_event_settlement_dates(_collect_event_ids(rows))

    by_profile: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_profile[row["profile_id"]].append(row)

    perfs: list[ProfilePerf] = []
    for profile_rows in by_profile.values():
        perf = _replay_profile(
            profile_rows,
            window=window,
            event_settlement_dates=event_settlement_dates,
        )
        if perf is not None:
            perfs.append(perf)

    return _attach_profile_meta(perfs, config)


def build_csv_rows(
    *,
    window: set[date] | None,
    config: Path,
) -> list[dict]:
    perfs = _load_perfs(window=window, config=config)
    rows: list[dict] = []
    for perf in sorted(perfs, key=lambda p: p.profile_id):
        since_s = perf.since.isoformat() if perf.since else ""
        lead_s = "" if perf.lead_hours is None else str(int(perf.lead_hours))
        for settlement_date in sorted(perf.daily):
            day = perf.daily[settlement_date]
            if day.n_trades <= 0:
                continue
            rows.append(
                {
                    "profile_id": perf.profile_id,
                    "model": perf.model_strategy,
                    "trade": perf.trade_strategy,
                    "lead_hours": lead_s,
                    "exit_mode": perf.exit_mode,
                    "event_budget": perf.event_budget,
                    "since": since_s,
                    "settlement_date": settlement_date.isoformat(),
                    "pnl_usd": round(day.pnl_usd, 4),
                    "pnl_pct": round(day.pct, 4),
                    "sod_balance_usd": round(day.sod_balance_usd, 4),
                    "n_trades": day.n_trades,
                }
            )
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    write_performance_daily_csv(path, rows)


def build_report(
    *,
    days: int,
    end: date,
    group: str,
    config: Path,
    min_settled_days: int,
    top: int | None,
    sort_by: str,
) -> str:
    window = _window_days(days=days, end=end)
    window_set = set(window)

    perfs = _load_perfs(window=window_set, config=config)

    if group == "trade":
        perfs = _rollup(
            perfs,
            key_fn=lambda p: p.trade_strategy,
            label_fn=lambda k, _: k,
        )
    elif group == "model_trade":
        perfs = _rollup(
            perfs,
            key_fn=lambda p: f"{p.model_strategy}/{p.trade_strategy}",
            label_fn=lambda k, _: k,
        )

    if min_settled_days > 0:
        perfs = [p for p in perfs if len(p.daily) >= min_settled_days]

    if sort_by == "7d":
        perfs.sort(key=lambda p: _sum_window_pct(p, window), reverse=True)
    elif sort_by == "balance":
        perfs.sort(key=lambda p: p.balance_usd, reverse=True)
    else:
        perfs.sort(key=lambda p: p.profile_id)

    if top is not None:
        perfs = perfs[:top]

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [
        f"# Paper wallet performance ({days}d ending {end.isoformat()})",
        "",
        f"generated_utc: {generated}",
        f"group: {group}",
        "",
        "Cell = realized P/L for that market settlement date as % of balance "
        "before the first realization that day (``SETTLE``/``CLOSE`` bucketed "
        "by Polymarket endDate, not ledger UTC timestamp). "
        "``·`` = no realizations that settlement date. ``since`` = first OPEN.",
        "",
        _render_table(
            perfs,
            window=window,
            title=f"Wallets ({len(perfs)} rows)",
        ),
        "",
    ]
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Trailing settlement dates (default 7)",
    )
    parser.add_argument(
        "--end",
        type=str,
        default="",
        help="Last settlement date inclusive (YYYY-MM-DD). Default: today UTC.",
    )
    parser.add_argument(
        "--group",
        choices=("profile", "trade", "model_trade"),
        default="profile",
        help="Row granularity (default: profile)",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_PROFILES_PATH)
    parser.add_argument(
        "--min-settled-days",
        type=int,
        default=0,
        help="Keep rows with at least N settlement days in the window",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=40,
        help="Max rows (default 40; 0 = all)",
    )
    parser.add_argument(
        "--sort",
        choices=("7d", "balance", "name"),
        default="7d",
        dest="sort_by",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write markdown here (default: stdout only)",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Write long-format CSV (use with --all for full history)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="CSV: every settlement date since first trade (ignores --days/--group/--top/--sort)",
    )
    args = parser.parse_args()

    end = date.fromisoformat(args.end) if args.end else datetime.now(timezone.utc).date()

    if args.csv:
        if args.all:
            window_set: set[date] | None = None
        else:
            window_set = set(_window_days(days=args.days, end=end))
        csv_rows = build_csv_rows(window=window_set, config=args.config)
        write_csv(args.csv, csv_rows)
        print(f"wrote {args.csv} ({len(csv_rows)} rows)")
        return 0

    top = None if args.top == 0 else args.top

    report = build_report(
        days=args.days,
        end=end,
        group=args.group,
        config=args.config,
        min_settled_days=args.min_settled_days,
        top=top,
        sort_by=args.sort_by,
    )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        # Report uses non-ASCII glyphs (Σ, ·, —); avoid crashing on a
        # legacy console encoding (e.g. cp1252 on Windows).
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
