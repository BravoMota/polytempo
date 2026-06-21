#!/usr/bin/env python3
"""Daily realized P/L matrix for paper wallets (profiles).

Rows: one wallet per profile (or rolled up by trade/model). Columns: calendar
days (default last 7 UTC). Cell: realized P/L that day as % of start-of-day
balance. Includes ``since`` (first OPEN) so new wallets are not compared on
stale cumulative totals alone.

Reads the live paper DB; no writes unless ``--out`` is set.
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

from polytempo.profiles.load import DEFAULT_PROFILES_PATH, load_paper_profiles  # noqa: E402
from polytempo.storage.paper_postgres import (  # noqa: E402
    STARTING_BALANCE_USD,
    get_paper_connection,
    resolve_paper_database_url,
)


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


@dataclass(frozen=True)
class DayPnl:
    pnl_usd: float
    sod_balance_usd: float

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


def _fetch_events(conn) -> list[dict]:
    return conn.execute(
        """
        SELECT id, profile_id, event_type, trade_id, ts_utc,
               stake_usd, payout_usd
        FROM paper_events
        ORDER BY profile_id, id ASC
        """
    ).fetchall()


def _replay_profile(
    rows: list[dict],
    *,
    window: set[date],
) -> ProfilePerf | None:
    if not rows:
        return None

    profile_id = rows[0]["profile_id"]
    balance = STARTING_BALANCE_USD
    open_stakes: dict[str, float] = {}
    daily_pnl_usd: dict[date, float] = defaultdict(float)
    sod_balance: dict[date, float] = {}
    since: date | None = None

    for row in rows:
        ts = _parse_ts(row["ts_utc"])
        day = ts.date()
        if since is None and row["event_type"] == "OPEN":
            since = day

        if day not in sod_balance:
            sod_balance[day] = balance

        kind = row["event_type"]
        if kind == "OPEN":
            stake = float(row["stake_usd"] or 0.0)
            balance -= stake
            open_stakes[row["trade_id"]] = stake
        elif kind in ("SETTLE", "CLOSE"):
            trade_id = row["trade_id"]
            stake = open_stakes.pop(trade_id, 0.0)
            payout = float(row["payout_usd"] or 0.0)
            balance += payout
            if day in window:
                daily_pnl_usd[day] += payout - stake

    daily = {
        d: DayPnl(pnl_usd=daily_pnl_usd[d], sod_balance_usd=sod_balance.get(d, balance))
        for d in window
        if d in daily_pnl_usd
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
            continue
        out.append(
            ProfilePerf(
                profile_id=perf.profile_id,
                trade_strategy=meta.trade_strategy,
                model_strategy=meta.model_strategy,
                since=perf.since,
                balance_usd=perf.balance_usd,
                daily=perf.daily,
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
            daily[d] = DayPnl(pnl_usd=pnl, sod_balance_usd=sod)
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

    url = resolve_paper_database_url()
    with get_paper_connection(url) as conn:
        rows = _fetch_events(conn)

    by_profile: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_profile[row["profile_id"]].append(row)

    perfs: list[ProfilePerf] = []
    for profile_rows in by_profile.values():
        perf = _replay_profile(profile_rows, window=window_set)
        if perf is not None:
            perfs.append(perf)

    perfs = _attach_profile_meta(perfs, config)

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
        "Cell = realized P/L that UTC day as % of start-of-day balance. "
        "``·`` = no settlements that day. ``since`` = first OPEN.",
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
    parser.add_argument("--days", type=int, default=7, help="Trailing UTC days (default 7)")
    parser.add_argument(
        "--end",
        type=str,
        default="",
        help="Last day inclusive (YYYY-MM-DD, UTC). Default: today UTC.",
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
    args = parser.parse_args()

    end = date.fromisoformat(args.end) if args.end else datetime.now(timezone.utc).date()
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
