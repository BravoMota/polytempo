#!/usr/bin/env python3
"""A/B report: active-sell (_xsell) wallets vs their hold-to-settle twins.

For every wallet declared in ``xsell_wallets`` it prints the active-sell wallet
beside its hold twin (same model x entry x gate, no exit policy): net P&L and,
for the active-sell side, how its positions left the book (TP / SL early close
vs settled at resolution) with the average realized exit ratio.

This is the artifact that answers "does the +50/-50 bracket help max_edge but
hurt dist_arb?". Reads the live paper DB; no writes.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polytempo.profiles.load import DEFAULT_PROFILES_PATH, load_paper_profiles  # noqa: E402
from polytempo.storage.paper_postgres import (  # noqa: E402
    get_paper_connection,
    resolve_paper_database_url,
)

STARTING = 1000.0


def _balances(conn, ids: list[str]) -> dict[str, float]:
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT profile_id, balance_usd FROM paper_profile_balances "
        "WHERE profile_id = ANY(%s)",
        (ids,),
    ).fetchall()
    return {r["profile_id"]: float(r["balance_usd"]) for r in rows}


def _exit_breakdown(conn, pid: str) -> dict:
    """CLOSE rows (TP/SL) joined to their OPEN for realized exit ratio, + settle count."""
    closes = conn.execute(
        """
        SELECT c.outcome, COUNT(*) AS n,
               AVG((c.metadata->>'sell_price')::float / NULLIF(o.entry_price, 0)) AS avg_ratio
        FROM paper_events c
        JOIN paper_events o ON o.trade_id = c.trade_id AND o.event_type = 'OPEN'
        WHERE c.event_type = 'CLOSE' AND c.profile_id = %s
        GROUP BY c.outcome
        """,
        (pid,),
    ).fetchall()
    settled = conn.execute(
        "SELECT COUNT(*) AS n FROM paper_events WHERE event_type='SETTLE' AND profile_id=%s",
        (pid,),
    ).fetchone()
    by = {r["outcome"]: (int(r["n"]), r["avg_ratio"]) for r in closes}
    return {
        "tp": by.get("TP", (0, None)),
        "sl": by.get("SL", (0, None)),
        "settled": int(settled["n"]) if settled else 0,
    }


def main() -> int:
    profiles = load_paper_profiles(DEFAULT_PROFILES_PATH)
    xsell = sorted(p.id for p in profiles if p.exit_policy is not None)
    if not xsell:
        print("No xsell_wallets declared in config.")
        return 0
    twins = [pid[: -len("_xsell")] for pid in xsell]

    url = resolve_paper_database_url()
    with get_paper_connection(url) as conn:
        bal = _balances(conn, xsell + twins)
        print(f"{'wallet':<34}{'net P&L':>10}{'twin net':>10}{'  exits (TP/SL/settle, avg ratio)'}")
        print("-" * 96)
        for pid, twin in zip(xsell, twins):
            x_net = bal.get(pid, STARTING) - STARTING
            t_net = bal.get(twin, STARTING) - STARTING
            ex = _exit_breakdown(conn, pid)
            (tp_n, tp_r), (sl_n, sl_r) = ex["tp"], ex["sl"]
            tp_s = f"{tp_n}@{tp_r:.2f}x" if tp_r is not None else f"{tp_n}"
            sl_s = f"{sl_n}@{sl_r:.2f}x" if sl_r is not None else f"{sl_n}"
            print(
                f"{pid:<34}{x_net:>+10.2f}{t_net:>+10.2f}   "
                f"TP {tp_s} / SL {sl_s} / settle {ex['settled']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
