#!/usr/bin/env python3
"""Initialize the PolyTempo paper trading PostgreSQL schema.

The target database is whatever ``POLYTEMPO_PAPER_DATABASE_URL`` (or
``--database-url``) points at, so each city keeps its own: ``polytempo_paper``
for London, ``polytempo_paper_madrid`` for Madrid (profile ids collide across
cities and ``paper_events`` has no city column, so one database per city).

    POLYTEMPO_PAPER_DATABASE_URL=postgresql://localhost/polytempo_paper_madrid \\
        python scripts/init_paper_db.py

The database itself must already exist (``createdb``). The schema is additive
(``CREATE TABLE IF NOT EXISTS`` / ``CREATE OR REPLACE VIEW``), so re-running
against a populated database is a no-op and never drops or clears data.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from polytempo.storage.paper_postgres import (  # noqa: E402
    initialize_paper_database,
    resolve_paper_database_url,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create paper trading PostgreSQL schema")
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override POLYTEMPO_PAPER_DATABASE_URL",
    )
    args = parser.parse_args()

    if not args.database_url and not os.environ.get("POLYTEMPO_PAPER_DATABASE_URL"):
        print(
            "POLYTEMPO_PAPER_DATABASE_URL is not set. There is no default: set it "
            "to the database you mean to initialize, e.g.\n"
            "  POLYTEMPO_PAPER_DATABASE_URL=postgresql://localhost/polytempo_paper_madrid"
            " python scripts/init_paper_db.py",
            file=sys.stderr,
        )
        return 2

    database_url = resolve_paper_database_url(override=args.database_url)
    initialize_paper_database(database_url)
    print(f"initialized paper database={database_url.split('@')[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
