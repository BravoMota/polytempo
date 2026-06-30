#!/usr/bin/env python3
"""Audit and remediate lowest-temperature Polymarket events stored by earlier bugs.

Polymarket runs parallel "Highest temperature in <city>" and "Lowest temperature in
<city>" daily markets. This project resolves daily highest (Tmax) markets only, but a
discovery bug let lowest-temperature events be selected, persisting wrong CLOB snapshots
and (potentially) paper trades against them.

This script classifies every Polymarket event id found in the weather and paper
databases, then optionally cleans them up:

- Audit (default): print each event id, its Gamma title, and row counts. No writes.
- ``--delete-clob``: delete ``clob_bucket_snapshots`` rows for lowest-temperature events
  (weather DB, ``POLYTEMPO_DATABASE_URL``).
- ``--flag-paper``: tag ``paper_events`` rows tied to lowest-temperature events with a
  ``market_kind_mismatch`` metadata marker (paper DB, ``POLYTEMPO_PAPER_DATABASE_URL``).
  Trades are never deleted or rewritten, only flagged for downstream exclusion.

Destructive flags require ``--yes`` (or an interactive confirmation).

Examples
--------
    # Audit only (safe)
    python scripts/remediate_lowest_temp_events.py

    # Delete bad CLOB snapshots and flag affected paper trades
    python scripts/remediate_lowest_temp_events.py --delete-clob --flag-paper --yes
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polytempo.markets.polymarket import (  # noqa: E402
    fetch_event,
    is_lowest_temperature_event,
)
from polytempo.storage.paper_postgres import (  # noqa: E402
    get_paper_connection,
    resolve_paper_database_url,
)
from polytempo.storage.postgres import (  # noqa: E402
    get_connection,
    resolve_database_url,
)


def _distinct_clob_event_ids(conn) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT polymarket_event_id AS event_id, COUNT(*) AS n
        FROM clob_bucket_snapshots
        GROUP BY polymarket_event_id
        """
    ).fetchall()
    return {str(r["event_id"]): int(r["n"]) for r in rows}


def _distinct_paper_event_ids(conn) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT polymarket_event_id AS event_id, COUNT(*) AS n
        FROM paper_events
        WHERE event_type = 'OPEN' AND polymarket_event_id IS NOT NULL
        GROUP BY polymarket_event_id
        """
    ).fetchall()
    return {str(r["event_id"]): int(r["n"]) for r in rows}


def _classify(event_ids: set[str]) -> dict[str, tuple[str, bool]]:
    """Map each event id to (title, is_lowest); title is "<unavailable>" on fetch error."""
    classified: dict[str, tuple[str, bool]] = {}
    for event_id in sorted(event_ids):
        try:
            event = fetch_event(event_id)
            classified[event_id] = (event.title, is_lowest_temperature_event(event))
        except Exception as exc:  # noqa: BLE001 - network/parse errors are reported, not fatal
            classified[event_id] = (f"<unavailable: {exc}>", False)
    return classified


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    reply = input(f"{prompt} [y/N] ").strip().lower()
    return reply in ("y", "yes")


def _delete_clob_rows(database_url: str, lowest_ids: list[str], assume_yes: bool) -> None:
    if not lowest_ids:
        print("delete-clob: no lowest-temperature CLOB snapshots to remove.")
        return
    if not _confirm(
        f"Delete CLOB snapshots for {len(lowest_ids)} lowest-temperature event(s)?",
        assume_yes,
    ):
        print("delete-clob: aborted.")
        return
    with get_connection(database_url) as conn:
        cur = conn.execute(
            "DELETE FROM clob_bucket_snapshots WHERE polymarket_event_id = ANY(%(ids)s)",
            {"ids": lowest_ids},
        )
        deleted = cur.rowcount
        conn.commit()
    print(f"delete-clob: removed {deleted} CLOB snapshot row(s).")


def _flag_paper_rows(database_url: str, lowest_ids: list[str], assume_yes: bool) -> None:
    if not lowest_ids:
        print("flag-paper: no lowest-temperature paper events to flag.")
        return
    if not _confirm(
        f"Flag paper_events for {len(lowest_ids)} lowest-temperature event(s)?",
        assume_yes,
    ):
        print("flag-paper: aborted.")
        return
    marker = json.dumps(
        {
            "market_kind_mismatch": "lowest_temperature",
            "remediated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    with get_paper_connection(database_url) as conn:
        cur = conn.execute(
            """
            UPDATE paper_events
            SET metadata = metadata || %(marker)s::jsonb
            WHERE polymarket_event_id = ANY(%(ids)s)
            """,
            {"marker": marker, "ids": lowest_ids},
        )
        flagged = cur.rowcount
        conn.commit()
    print(f"flag-paper: flagged {flagged} paper_events row(s).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--delete-clob",
        action="store_true",
        help="Delete clob_bucket_snapshots rows for lowest-temperature events.",
    )
    parser.add_argument(
        "--flag-paper",
        action="store_true",
        help="Tag paper_events rows tied to lowest-temperature events.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation for destructive actions.",
    )
    args = parser.parse_args()

    weather_url = resolve_database_url()
    with get_connection(weather_url) as conn:
        clob_counts = _distinct_clob_event_ids(conn)

    paper_url: str | None = None
    paper_counts: dict[str, int] = {}
    try:
        paper_url = resolve_paper_database_url()
        with get_paper_connection(paper_url) as conn:
            paper_counts = _distinct_paper_event_ids(conn)
    except RuntimeError:
        # Paper DB not configured: audit weather-only unless --flag-paper requires it.
        if args.flag_paper:
            raise
        print("(paper DB not configured; auditing CLOB snapshots only)")

    all_ids = set(clob_counts) | set(paper_counts)
    if not all_ids:
        print("No Polymarket event ids found in either database.")
        return

    classified = _classify(all_ids)

    print(f"{'event_id':<12} {'kind':<8} {'clob':>7} {'paper':>7}  title")
    print("-" * 72)
    lowest_ids: list[str] = []
    for event_id in sorted(all_ids):
        title, is_lowest = classified[event_id]
        if is_lowest:
            lowest_ids.append(event_id)
        kind = "LOWEST" if is_lowest else "highest"
        print(
            f"{event_id:<12} {kind:<8} {clob_counts.get(event_id, 0):>7} "
            f"{paper_counts.get(event_id, 0):>7}  {title}"
        )

    print("-" * 72)
    print(f"Lowest-temperature events: {len(lowest_ids)} / {len(all_ids)} total")

    if args.delete_clob:
        _delete_clob_rows(weather_url, lowest_ids, args.yes)
    if args.flag_paper and paper_url is not None:
        _flag_paper_rows(paper_url, lowest_ids, args.yes)

    if not args.delete_clob and not args.flag_paper:
        print(
            "\nAudit only. Re-run with --delete-clob and/or --flag-paper to remediate."
        )


if __name__ == "__main__":
    main()
